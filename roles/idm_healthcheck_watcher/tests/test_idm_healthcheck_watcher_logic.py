#!/usr/bin/env python3
"""
Standalone, throwaway logic test for idm_healthcheck_watcher.py - lives
in roles/idm_healthcheck_watcher/tests/, NOT
roles/idm_healthcheck_watcher/files/, so it is never deployed by
Ansible (tasks/main.yml only ever copies the single named file
files/idm_healthcheck_watcher.py to a host - nothing copies this
directory at all).

Exercises the bootstrap-without-alerting behaviour, the dedup/state-
advance behaviour, the row-cap/truncation-note behaviour, both message
shapes (per-finding issue and RUN_FAILED), the syslogtag LIKE-pattern
query filter, worst-severity subject selection, and hostname
de-linking in the HTML body, against a minimal fake psycopg2
connection/cursor - no real PostgreSQL required.

Run directly:  python3 roles/idm_healthcheck_watcher/tests/test_idm_healthcheck_watcher_logic.py
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
            assert like_pattern == "idm_healthcheck_watch[%"
            self._last_rows = [
                r for r in self._rows_by_query["rows"]
                if r["id"] > last_id and r["syslogtag"].startswith("idm_healthcheck_watch[")
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


def make_issue_row(row_id, host="idm2.lab.example.com", source="ipahealthcheck.meta.services",
                    check="ServiceCheck", severity="WARNING", msg="service degraded"):
    return {
        "id": row_id,
        "message": "idm_healthcheck_watch: %s.%s %s: %s" % (source, check, severity, msg),
        "fromhost": host,
        "devicereportedtime": "2026-09-11 01:00:0%d" % (row_id % 10),
        "syslogtag": "idm_healthcheck_watch[%d]:" % (20000 + row_id),
    }


def make_run_failed_row(row_id, host="idm3.lab.example.com", reason="ipa-healthcheck timed out after 180s"):
    return {
        "id": row_id,
        "message": "idm_healthcheck_watch: RUN_FAILED: %s" % reason,
        "fromhost": host,
        "devicereportedtime": "2026-09-11 02:00:0%d" % (row_id % 10),
        "syslogtag": "idm_healthcheck_watch[%d]:" % (30000 + row_id),
    }


class IdmHealthcheckWatcherLogicTest(unittest.TestCase):
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

        import idm_healthcheck_watcher  # noqa: E402
        import importlib
        importlib.reload(idm_healthcheck_watcher)
        self.watcher = idm_healthcheck_watcher

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

    def test_second_run_alerts_only_on_new_findings_and_advances_state(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 42}, fh)

        new_rows = [
            make_issue_row(43, severity="CRITICAL", check="IPACertNSSTrust", msg="cert expired"),
            make_issue_row(44, host="idm1.lab.example.com", severity="WARNING",
                            check="ReplicationCheck", msg="2 conflicts"),
        ]
        rows_by_query = {"max_id": 44, "rows": new_rows}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertIn("2 IdM healthcheck issue(s) detected across", subject)
        self.assertIn("CRITICAL", subject)  # worst severity surfaces in the multi-row subject
        self.assertIn("IPACertNSSTrust", body)
        self.assertIn("ReplicationCheck", body)
        self.assertIn("<table", html_body)
        # Host column has a zero-width space before every '.' so mail
        # clients don't auto-link the hostname into a clickable
        # http://... URL - renders visually identical to the plain
        # hostname either way.
        self.assertNotIn("idm2.lab.example.com", html_body)
        self.assertIn("idm2\u200b.lab\u200b.example\u200b.com", html_body)
        self.assertIn("format-detection", html_body)

        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 44)

    def test_single_issue_uses_host_source_check_in_subject(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        rows_by_query = {
            "max_id": 1,
            "rows": [make_issue_row(1, host="idm1.lab.example.com",
                                     source="ipahealthcheck.ds.replication",
                                     check="ReplicationConflictCheck",
                                     severity="CRITICAL", msg="2 conflicts found")],
        }
        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertEqual(
            subject,
            "[idm1.lab.example.com] IdM healthcheck ipahealthcheck.ds.replication.ReplicationConflictCheck: CRITICAL",
        )
        self.assertIn("2 conflicts found", body)
        self.assertIn(">CRITICAL<", html_body)

    def test_run_failed_row_produces_run_failed_subject_and_critical_styling(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        rows_by_query = {"max_id": 1, "rows": [make_run_failed_row(1)]}
        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertEqual(subject, "[idm3.lab.example.com] ipa-healthcheck run failed")
        self.assertIn("timed out after 180s", body)
        self.assertIn("color:#a13544", html_body)  # styled as CRITICAL-severity red

    def test_no_new_findings_sends_nothing_and_state_unchanged(self):
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

        rows = [make_issue_row(i) for i in range(1, 13)]  # 12 rows, cap is 5
        rows_by_query = {"max_id": 12, "rows": rows}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertIn("12 IdM healthcheck issue(s)", subject)
        self.assertIn("7 more not shown", body)
        self.assertIn("7 more not shown", html_body)

        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 12)

    def test_non_matching_rows_are_excluded_by_the_query_filter(self):
        # FakeCursor itself enforces the syslogtag-prefix match
        # (mirroring the real SQL LIKE filter) - this test just
        # confirms a mixed batch only alerts on the matching rows.
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        matching = [make_issue_row(1)]
        non_matching = [{
            "id": 2, "message": "systemd-logind: some routine notice",
            "fromhost": "idm2.lab.example.com",
            "devicereportedtime": "2026-09-11 01:00:02",
            "syslogtag": "systemd-logind",
        }]
        rows_by_query = {"max_id": 2, "rows": matching + non_matching}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        # state must still advance to the true max id among matching
        # rows only (1, since row 2 never matched the query filter)
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 1)

    def test_parse_alert_falls_back_gracefully_on_unexpected_wording(self):
        source, check, severity, detail = self.watcher.parse_alert(
            "this does not match either expected shape"
        )
        self.assertIsNone(source)
        self.assertIsNone(check)
        self.assertEqual(severity, "UNKNOWN")

        source, check, severity, detail = self.watcher.parse_alert(
            "idm_healthcheck_watch: ipahealthcheck.meta.core.MetaCheck ERROR: unexpected exception"
        )
        self.assertEqual(source, "ipahealthcheck.meta.core")
        self.assertEqual(check, "MetaCheck")
        self.assertEqual(severity, "ERROR")
        self.assertEqual(detail, "unexpected exception")

    def test_parse_alert_run_failed_shape(self):
        source, check, severity, detail = self.watcher.parse_alert(
            "idm_healthcheck_watch: RUN_FAILED: could not run ipa-healthcheck: not found"
        )
        self.assertEqual(source, "(n/a)")
        self.assertEqual(check, "(n/a)")
        self.assertEqual(severity, "RUN_FAILED")
        self.assertEqual(detail, "could not run ipa-healthcheck: not found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
