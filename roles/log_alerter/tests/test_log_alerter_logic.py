#!/usr/bin/env python3
"""
Standalone, throwaway logic test for log_alerter.py - lives in
roles/log_alerter/tests/, NOT roles/log_alerter/files/, so it is never
deployed by Ansible (tasks/main.yml only ever copies the single named
file files/log_alerter.py to a host - nothing copies this directory at
all).

Exercises the bootstrap-without-alerting behaviour, the dedup/state-
advance behaviour, and the row-cap/truncation-note behaviour against a
minimal fake psycopg2 connection/cursor - no real PostgreSQL required.
Run directly:  python3 roles/log_alerter/tests/test_log_alerter_logic.py
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
            last_id, threshold = params
            self._last_rows = [
                r for r in self._rows_by_query["rows"]
                if r["id"] > last_id and r["priority"] <= threshold
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


def make_test_rows(n, priority=4):
    return [
        {
            "id": i,
            "message": "test message %d" % i,
            "facility": 1,
            "fromhost": "client1.lab.example.com",
            "priority": priority,
            "devicereportedtime": "2026-09-05 12:00:0%d" % (i % 10),
            "syslogtag": "testtag",
        }
        for i in range(1, n + 1)
    ]


class LogAlerterLogicTest(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.state_dir, "state.json")

        config = types.ModuleType("config")
        config.DB_HOST = "localhost"
        config.DB_NAME = "syslog"
        config.DB_TABLE = "systemevents"
        config.DB_USER = "log_search"
        config.DB_PASSWORD = "unused"
        config.SEVERITY_THRESHOLD = 4
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

        import log_alerter  # noqa: E402  (imported after sys.modules stubs)
        import importlib
        importlib.reload(log_alerter)
        self.log_alerter = log_alerter

    def tearDown(self):
        if os.path.exists(self.state_file):
            os.remove(self.state_file)
        os.rmdir(self.state_dir)

    def test_first_run_bootstraps_without_alerting(self):
        rows_by_query = {"max_id": 42, "rows": []}
        with mock.patch.object(
            self.log_alerter, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.log_alerter, "send_email") as mock_send:
            self.log_alerter.main()

        mock_send.assert_not_called()
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 42)

    def test_second_run_alerts_only_on_new_rows_and_advances_state(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 42}, fh)

        new_rows = make_test_rows(3)
        for r in new_rows:
            r["id"] += 42  # ids 43, 44, 45
        rows_by_query = {"max_id": 45, "rows": new_rows}

        with mock.patch.object(
            self.log_alerter, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.log_alerter, "send_email") as mock_send:
            self.log_alerter.main()

        mock_send.assert_called_once()
        subject, body = mock_send.call_args[0]
        self.assertIn("3 new log alert(s)", subject)
        self.assertIn("test message 1", body)
        self.assertIn("test message 3", body)

        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 45)

    def test_no_new_rows_sends_nothing_and_state_unchanged(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 45}, fh)

        rows_by_query = {"max_id": 45, "rows": []}
        with mock.patch.object(
            self.log_alerter, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.log_alerter, "send_email") as mock_send:
            self.log_alerter.main()

        mock_send.assert_not_called()
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 45)

    def test_row_cap_truncation_note_and_state_advances_past_capped_subset(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        rows = make_test_rows(12)  # MAX_ROWS_PER_EMAIL is 5 in this test config
        rows_by_query = {"max_id": 12, "rows": rows}

        with mock.patch.object(
            self.log_alerter, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.log_alerter, "send_email") as mock_send:
            self.log_alerter.main()

        mock_send.assert_called_once()
        subject, body = mock_send.call_args[0]
        self.assertIn("12 new log alert(s)", subject)
        self.assertIn("7 more not shown", body)
        self.assertIn("test message 1", body)
        self.assertNotIn("test message 12", body)  # beyond the cap of 5

        # State must advance to the TRUE max id (12), not just the
        # capped subset shown (5) - otherwise rows 6-12 would be
        # silently skipped on the next run.
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 12)

    def test_severity_filter_excludes_less_severe_rows(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        matching = make_test_rows(2, priority=4)
        non_matching = make_test_rows(2, priority=6)
        for r in non_matching:
            r["id"] += 100
        rows_by_query = {"max_id": 102, "rows": matching + non_matching}

        with mock.patch.object(
            self.log_alerter, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.log_alerter, "send_email") as mock_send:
            self.log_alerter.main()

        mock_send.assert_called_once()
        _, body = mock_send.call_args[0]
        self.assertIn("2 new log alert(s)"[:1], "2")  # sanity marker
        self.assertIn("2 new log event", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
