#!/usr/bin/env python3
"""
Standalone, throwaway logic test for service_failure_watcher.py - lives
in roles/service_failure_watcher/tests/, NOT roles/service_failure_watcher/
files/, so it is never deployed by Ansible (tasks/main.yml only ever
copies the single named file files/service_failure_watcher.py to a
host - nothing copies this directory at all).

Exercises the bootstrap-without-alerting behaviour, the dedup/state-
advance behaviour, the row-cap/truncation-note behaviour, the unit/
result parsing, and the LIKE-pattern query filter against a minimal
fake psycopg2 connection/cursor - no real PostgreSQL required.
Run directly:  python3 roles/service_failure_watcher/tests/test_service_failure_watcher_logic.py
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


class FakeCursor:
    def __init__(self, rows_by_query):
        self._rows_by_query = rows_by_query
        self._last_rows = []

    def execute(self, query, params=None):
        if "MAX(id)" in query:
            self._last_rows = [{"max_id": self._rows_by_query["max_id"]}]
        elif "WHERE id >" in query:
            last_id, like_pattern = params
            assert like_pattern == "%: Failed with result %"
            self._last_rows = [
                r for r in self._rows_by_query["rows"]
                if r["id"] > last_id and "Failed with result" in r["message"]
            ]

    def fetchone(self):
        return self._last_rows[0]

    def fetchall(self):
        return self._last_rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, rows_by_query):
        self._rows_by_query = rows_by_query

    def cursor(self, cursor_factory=None):
        return FakeCursor(self._rows_by_query)

    def close(self):
        pass


def make_failure_row(row_id, host="client1.lab.example.com",
                      unit="chronyd.service", result="signal"):
    return {
        "id": row_id,
        "message": "%s: Failed with result '%s'." % (unit, result),
        "fromhost": host,
        "devicereportedtime": "2026-09-05 12:00:0%d" % (row_id % 10),
        "syslogtag": "systemd",
    }


class ServiceFailureWatcherLogicTest(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.state_dir, "state.json")

        config = types.ModuleType("config")
        config.DB_HOST = "localhost"
        config.DB_NAME = "syslog"
        config.DB_TABLE = "systemevents"
        config.DB_USER = "log_search"
        config.DB_PASSWORD = "unused"
        config.MAX_ROWS_PER_EMAIL = 5
        config.STATE_FILE = self.state_file
        config.EMAIL_TO = "root"
        config.EMAIL_FROM = "rhel-alerts@test.example.com"
        config.SMTP_HOST = "localhost"
        config.SMTP_PORT = 25
        sys.modules["config"] = config

        sys.modules.setdefault("psycopg2", mock.MagicMock())
        sys.modules.setdefault(
            "psycopg2.extras", mock.MagicMock(RealDictCursor=object())
        )

        import service_failure_watcher  # noqa: E402
        import importlib
        importlib.reload(service_failure_watcher)
        self.watcher = service_failure_watcher

    def tearDown(self):
        if os.path.exists(self.state_file):
            os.remove(self.state_file)
        os.rmdir(self.state_dir)

    def test_first_run_bootstraps_without_alerting(self):
        rows_by_query = {"max_id": 42, "rows": []}
        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_not_called()
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 42)

    def test_second_run_alerts_only_on_new_failures_and_advances_state(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 42}, fh)

        new_rows = [make_failure_row(43), make_failure_row(44, unit="sshd.service", result="exit-code")]
        rows_by_query = {"max_id": 44, "rows": new_rows}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertIn("2 service failure(s) detected across", subject)
        self.assertIn("chronyd.service", body)
        self.assertIn("sshd.service", body)
        self.assertIn("result=signal", body)
        self.assertIn("result=exit-code", body)
        self.assertIn("chronyd.service", html_body)
        self.assertIn("sshd.service", html_body)
        self.assertIn("<table", html_body)
        # Host column and raw log lines have a zero-width space
        # before every '.' so mail clients don't auto-link the
        # hostname into a clickable http://... URL - renders
        # visually identical to the plain hostname either way.
        self.assertNotIn("client1.lab.example.com", html_body)
        self.assertIn("client1\u200b.lab\u200b.example\u200b.com", html_body)
        self.assertIn("format-detection", html_body)

        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 44)

    def test_single_failure_uses_host_and_unit_in_subject(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        rows_by_query = {
            "max_id": 1,
            "rows": [make_failure_row(1, host="idm1.lab.example.com", unit="sssd.service", result="timeout")],
        }
        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertEqual(subject, "[idm1.lab.example.com] sssd.service FAILED")
        self.assertIn("result=timeout", body)
        self.assertIn(">timeout<", html_body)

    def test_no_new_failures_sends_nothing_and_state_unchanged(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 45}, fh)

        rows_by_query = {"max_id": 45, "rows": []}
        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_not_called()
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 45)

    def test_row_cap_truncation_note_and_state_advances_past_capped_subset(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        rows = [make_failure_row(i) for i in range(1, 13)]  # 12 rows, cap is 5
        rows_by_query = {"max_id": 12, "rows": rows}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertIn("12 service failure(s)", subject)
        self.assertIn("7 more not shown", body)
        self.assertIn("7 more not shown", html_body)

        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 12)

    def test_non_failure_rows_are_excluded_by_the_query_filter(self):
        # FakeCursor itself enforces the "Failed with result" substring
        # match (mirroring the real SQL LIKE filter) - this test just
        # confirms a mixed batch only alerts on the matching rows.
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        matching = [make_failure_row(1)]
        non_matching = [{
            "id": 2, "message": "systemd-logind: some routine notice",
            "fromhost": "client1.lab.example.com",
            "devicereportedtime": "2026-09-05 12:00:02",
            "syslogtag": "systemd-logind",
        }]
        rows_by_query = {"max_id": 2, "rows": matching + non_matching}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, _body, _html_body = mock_send.call_args[0]
        self.assertEqual(subject, "[client1.lab.example.com] chronyd.service FAILED")
        # state must still advance to the true max id among matching
        # rows only (1, since row 2 never matched the query filter)
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 1)

    def test_parse_failure_falls_back_gracefully_on_unexpected_wording(self):
        unit, result = self.watcher.parse_failure("this does not match the expected format")
        self.assertIsNone(unit)
        self.assertIsNone(result)

        unit, result = self.watcher.parse_failure("nfs-server.service: Failed with result 'timeout'.")
        self.assertEqual(unit, "nfs-server.service")
        self.assertEqual(result, "timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
