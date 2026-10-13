#!/usr/bin/env python3
"""
Standalone, throwaway logic test for chrony_check.py - lives in
roles/chrony_check/tests/, NOT roles/chrony_check/files/, so it is
never deployed by Ansible (tasks/main.yml only ever copies the single
named file files/chrony_check.py to a host - nothing copies this
directory at all).

Exercises each of the three checks (NTP sync, chronyd active, chrony
leap status) against a stubbed subprocess.run(), the Leap-status-line
parser, the end-to-end check_all() issue list, and main()'s syslog
emission (including the no-alert / nothing-logged case) - no real
chronyd/timedatectl required.

Run directly:  python3 roles/chrony_check/tests/test_chrony_check_logic.py
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

_ROLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROLE_DIR, "files"))

import chrony_check  # noqa: E402


def make_completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


class ChronyCheckLogicTest(unittest.TestCase):
    # -- check_ntp_synchronized() ---------------------------------------

    def test_ntp_synchronized_yes_is_healthy(self):
        with mock.patch.object(
            chrony_check.subprocess, "run",
            return_value=make_completed(0, "yes\n"),
        ):
            self.assertIsNone(chrony_check.check_ntp_synchronized())

    def test_ntp_synchronized_no_is_an_issue(self):
        with mock.patch.object(
            chrony_check.subprocess, "run",
            return_value=make_completed(0, "no\n"),
        ):
            issue = chrony_check.check_ntp_synchronized()
        self.assertEqual(
            issue, "NTP not synchronized (timedatectl NTPSynchronized=no)"
        )

    def test_ntp_synchronized_missing_binary_is_unknown_not_an_issue(self):
        with mock.patch.object(
            chrony_check.subprocess, "run", side_effect=OSError("not found"),
        ):
            self.assertIsNone(chrony_check.check_ntp_synchronized())

    # -- check_chronyd_active() -----------------------------------------

    def test_chronyd_active_is_healthy(self):
        with mock.patch.object(
            chrony_check.subprocess, "run",
            return_value=make_completed(0, "active\n"),
        ):
            self.assertIsNone(chrony_check.check_chronyd_active())

    def test_chronyd_inactive_is_an_issue(self):
        with mock.patch.object(
            chrony_check.subprocess, "run",
            return_value=make_completed(3, "inactive\n"),
        ):
            issue = chrony_check.check_chronyd_active()
        self.assertEqual(
            issue, "chronyd service not active (systemctl status: inactive)"
        )

    def test_chronyd_failed_is_an_issue(self):
        with mock.patch.object(
            chrony_check.subprocess, "run",
            return_value=make_completed(3, "failed\n"),
        ):
            issue = chrony_check.check_chronyd_active()
        self.assertEqual(
            issue, "chronyd service not active (systemctl status: failed)"
        )

    def test_chronyd_timeout_is_unknown_not_an_issue(self):
        with mock.patch.object(
            chrony_check.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=10),
        ):
            self.assertIsNone(chrony_check.check_chronyd_active())

    # -- parse_leap_status() --------------------------------------------

    def test_parse_leap_status_normal(self):
        tracking = (
            "Reference ID    : C0A80101 (192.168.1.1)\n"
            "Stratum         : 3\n"
            "Leap status     : Normal\n"
        )
        self.assertEqual(chrony_check.parse_leap_status(tracking), "Normal")

    def test_parse_leap_status_insert_second(self):
        tracking = "Leap status     : Insert second\n"
        self.assertEqual(
            chrony_check.parse_leap_status(tracking), "Insert second"
        )

    def test_parse_leap_status_missing_line_returns_none(self):
        self.assertIsNone(chrony_check.parse_leap_status("Stratum : 3\n"))

    # -- check_chrony_leap() ----------------------------------------------

    def test_chrony_leap_normal_is_healthy(self):
        with mock.patch.object(
            chrony_check.subprocess, "run",
            return_value=make_completed(0, "Leap status     : Normal\n"),
        ):
            self.assertIsNone(chrony_check.check_chrony_leap())

    def test_chrony_leap_abnormal_is_an_issue(self):
        with mock.patch.object(
            chrony_check.subprocess, "run",
            return_value=make_completed(0, "Leap status     : Not synchronised\n"),
        ):
            issue = chrony_check.check_chrony_leap()
        self.assertEqual(
            issue,
            "chrony leap status abnormal (Not synchronised, expected Normal)",
        )

    def test_chrony_leap_nonzero_exit_is_unknown_not_an_issue(self):
        with mock.patch.object(
            chrony_check.subprocess, "run",
            return_value=make_completed(1, ""),
        ):
            self.assertIsNone(chrony_check.check_chrony_leap())

    # -- check_all() end-to-end ------------------------------------------

    def test_check_all_reports_no_issues_when_everything_healthy(self):
        responses = {
            ("timedatectl", "show", "-p", "NTPSynchronized", "--value"): make_completed(0, "yes"),
            ("systemctl", "is-active", "chronyd"): make_completed(0, "active"),
            ("chronyc", "tracking"): make_completed(0, "Leap status     : Normal"),
        }

        def fake_run(argv, **_kwargs):
            return responses[tuple(argv)]

        with mock.patch.object(chrony_check.subprocess, "run", side_effect=fake_run):
            self.assertEqual(chrony_check.check_all(), [])

    def test_check_all_reports_all_three_issues_when_all_breach(self):
        responses = {
            ("timedatectl", "show", "-p", "NTPSynchronized", "--value"): make_completed(0, "no"),
            ("systemctl", "is-active", "chronyd"): make_completed(3, "inactive"),
            ("chronyc", "tracking"): make_completed(0, "Leap status     : Insert second"),
        }

        def fake_run(argv, **_kwargs):
            return responses[tuple(argv)]

        with mock.patch.object(chrony_check.subprocess, "run", side_effect=fake_run):
            issues = chrony_check.check_all()
        self.assertEqual(len(issues), 3)
        self.assertIn("NTP not synchronized", issues[0])
        self.assertIn("chronyd service not active", issues[1])
        self.assertIn("chrony leap status abnormal", issues[2])

    # -- format_message() -------------------------------------------------

    def test_format_message_wording(self):
        msg = chrony_check.format_message("chronyd service not active (systemctl status: failed)")
        self.assertEqual(
            msg,
            "chrony_check: chronyd service not active (systemctl status: failed)",
        )

    # -- main() / syslog emission -----------------------------------------

    def test_main_emits_no_syslog_calls_when_nothing_breaches(self):
        with mock.patch.object(chrony_check, "check_all", return_value=[]), \
                mock.patch.object(chrony_check, "syslog") as mock_syslog:
            chrony_check.main()
        mock_syslog.openlog.assert_not_called()
        mock_syslog.syslog.assert_not_called()

    def test_main_emits_one_syslog_call_per_issue_at_warning_priority(self):
        issues = [
            "NTP not synchronized (timedatectl NTPSynchronized=no)",
            "chronyd service not active (systemctl status: failed)",
        ]
        with mock.patch.object(chrony_check, "check_all", return_value=issues), \
                mock.patch.object(chrony_check, "syslog") as mock_syslog:
            mock_syslog.LOG_WARNING = "WARN_PRIO"
            mock_syslog.LOG_PID = 1
            mock_syslog.LOG_DAEMON = 3
            chrony_check.main()

        mock_syslog.openlog.assert_called_once()
        self.assertEqual(mock_syslog.syslog.call_count, 2)
        first_call, second_call = mock_syslog.syslog.call_args_list
        self.assertEqual(first_call.args[0], "WARN_PRIO")
        self.assertIn("NTP not synchronized", first_call.args[1])
        self.assertEqual(second_call.args[0], "WARN_PRIO")
        self.assertIn("chronyd service not active", second_call.args[1])
        mock_syslog.closelog.assert_called_once()


if __name__ == "__main__":
    unittest.main()
