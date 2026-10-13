#!/usr/bin/env python3
"""
Standalone, throwaway logic test for host_availability_watcher.py -
lives in roles/host_availability_watcher/tests/, NOT roles/
host_availability_watcher/files/, so it is never deployed by Ansible
(tasks/main.yml only ever copies the single named file files/
host_availability_watcher.py to a host - nothing copies this directory
at all).

Exercises the UP/DOWN/SSH_UNREACHABLE state classification, the
consecutive-failure threshold/hysteresis behaviour (no alert on a
single blip), the one-email-per-transition dedup (no re-alert while a
state persists), the recovery alert, the NOTIFY_RECOVERY toggle, and
batching multiple simultaneous host transitions into one email - all
against mocked check_ping()/check_ssh_port() calls, no real network
access or ping binary required.
Run directly:  python3 roles/host_availability_watcher/tests/test_host_availability_watcher_logic.py
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

_ROLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROLE_DIR, "files"))


class HostAvailabilityWatcherLogicTest(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.state_dir, "state.json")

        config = types.ModuleType("config")
        config.HOSTS = ["client1.lab.example.com", "nfs1.lab.example.com"]
        config.PING_COUNT = 2
        config.PING_TIMEOUT_SECONDS = 2
        config.SSH_PORT = 22
        config.SSH_TIMEOUT_SECONDS = 3
        config.CONSECUTIVE_FAILURES_THRESHOLD = 2
        config.NOTIFY_RECOVERY = True
        config.STATE_FILE = self.state_file
        config.EMAIL_TO = "root"
        config.EMAIL_FROM = "rhel-alerts@test.example.com"
        config.SMTP_HOST = "localhost"
        config.SMTP_PORT = 25
        sys.modules["config"] = config

        import host_availability_watcher  # noqa: E402
        import importlib
        importlib.reload(host_availability_watcher)
        self.watcher = host_availability_watcher

    def tearDown(self):
        if os.path.exists(self.state_file):
            os.remove(self.state_file)
        os.rmdir(self.state_dir)

    def _run_with(self, ping_ok_map, ssh_ok_map, mock_send):
        with mock.patch.object(
            self.watcher, "check_ping",
            side_effect=lambda h: ping_ok_map.get(h, True),
        ), mock.patch.object(
            self.watcher, "check_ssh_port",
            side_effect=lambda h: ssh_ok_map.get(h, True),
        ), mock.patch.object(self.watcher, "send_email", mock_send):
            self.watcher.main()

    def test_classify_all_combinations(self):
        self.assertEqual(self.watcher.classify(True, True), self.watcher.STATE_UP)
        self.assertEqual(self.watcher.classify(False, False), self.watcher.STATE_DOWN)
        self.assertEqual(self.watcher.classify(True, False), self.watcher.STATE_SSH_UNREACHABLE)
        self.assertEqual(self.watcher.classify(False, True), self.watcher.STATE_SSH_UNREACHABLE)

    def test_single_blip_does_not_alert_below_threshold(self):
        mock_send = mock.MagicMock()
        both_down = {"client1.lab.example.com": False}
        self._run_with(both_down, both_down, mock_send)  # poll 1: fail count -> 1, threshold is 2
        mock_send.assert_not_called()
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["client1.lab.example.com"]["state"], self.watcher.STATE_UP)
        self.assertEqual(state["client1.lab.example.com"]["ping_fail_count"], 1)

    def test_sustained_outage_alerts_once_after_threshold_and_not_again(self):
        mock_send = mock.MagicMock()
        both_down = {"client1.lab.example.com": False}
        self._run_with(both_down, both_down, mock_send)  # poll 1: count=1, still UP
        self._run_with(both_down, both_down, mock_send)  # poll 2: count=2 -> DOWN, alert
        mock_send.assert_called_once()
        subject, _body, _html_body = mock_send.call_args[0]
        self.assertIn("client1.lab.example.com", subject)
        self.assertIn("DOWN", subject)

        mock_send.reset_mock()
        self._run_with(both_down, both_down, mock_send)  # poll 3: still DOWN, no re-alert
        mock_send.assert_not_called()

    def test_recovery_sends_back_up_alert(self):
        mock_send = mock.MagicMock()
        both_down = {"client1.lab.example.com": False}
        both_up = {"client1.lab.example.com": True}
        self._run_with(both_down, both_down, mock_send)
        self._run_with(both_down, both_down, mock_send)  # now DOWN
        mock_send.reset_mock()
        self._run_with(both_up, both_up, mock_send)  # single successful poll clears both fail counts
        mock_send.assert_called_once()
        subject, _body, _html_body = mock_send.call_args[0]
        self.assertIn("back UP", subject)

    def test_notify_recovery_false_suppresses_recovery_email_but_state_still_updates(self):
        self.watcher.config.NOTIFY_RECOVERY = False
        mock_send = mock.MagicMock()
        both_down = {"client1.lab.example.com": False}
        both_up = {"client1.lab.example.com": True}
        self._run_with(both_down, both_down, mock_send)
        self._run_with(both_down, both_down, mock_send)  # DOWN, alert sent
        mock_send.reset_mock()
        self._run_with(both_up, both_up, mock_send)
        mock_send.assert_not_called()
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["client1.lab.example.com"]["state"], self.watcher.STATE_UP)

    def test_ping_ok_ssh_down_alerts_ssh_unreachable_not_down(self):
        mock_send = mock.MagicMock()
        ping_ok = {"client1.lab.example.com": True}
        ssh_down = {"client1.lab.example.com": False}
        self._run_with(ping_ok, ssh_down, mock_send)
        self._run_with(ping_ok, ssh_down, mock_send)
        mock_send.assert_called_once()
        subject, _body, _html_body = mock_send.call_args[0]
        self.assertIn("SSH_UNREACHABLE", subject)
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["client1.lab.example.com"]["state"], self.watcher.STATE_SSH_UNREACHABLE)

    def test_multiple_simultaneous_transitions_batch_into_one_email(self):
        mock_send = mock.MagicMock()
        both_down = {"client1.lab.example.com": False, "nfs1.lab.example.com": False}
        self._run_with(both_down, both_down, mock_send)
        self._run_with(both_down, both_down, mock_send)
        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertIn("2 host(s)", subject)
        self.assertIn("client1.lab.example.com", body)
        self.assertIn("nfs1.lab.example.com", body)
        # Hostnames in the HTML table have a zero-width space before
        # every '.' so mail clients' auto-linkers (e.g. Apple Mail's
        # Data Detectors) don't turn the plain hostname into a
        # clickable link - renders visually identical either way.
        self.assertNotIn("client1.lab.example.com", html_body)
        self.assertNotIn("nfs1.lab.example.com", html_body)
        self.assertIn("client1\u200b.lab\u200b.example\u200b.com", html_body)
        self.assertIn("nfs1\u200b.lab\u200b.example\u200b.com", html_body)
        self.assertIn("<table", html_body)
        self.assertIn("format-detection", html_body)

    def test_first_ever_run_with_no_prior_state_file_does_not_crash(self):
        # No state file exists yet at all (fresh deploy) - should behave
        # like every host started from STATE_UP with zero fail counts.
        self.assertFalse(os.path.exists(self.state_file))
        mock_send = mock.MagicMock()
        all_up = {}
        self._run_with(all_up, all_up, mock_send)
        mock_send.assert_not_called()
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["client1.lab.example.com"]["state"], self.watcher.STATE_UP)
        self.assertEqual(state["nfs1.lab.example.com"]["state"], self.watcher.STATE_UP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
