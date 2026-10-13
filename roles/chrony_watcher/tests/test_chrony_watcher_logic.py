#!/usr/bin/env python3
"""
Standalone, throwaway logic test for chrony_watcher.py - lives in
roles/chrony_watcher/tests/, NOT roles/chrony_watcher/files/, so it is
never deployed by Ansible (tasks/main.yml only ever copies the single
named file files/chrony_watcher.py to a host - nothing copies this
directory at all).

Exercises the bootstrap-without-alerting behaviour, the dedup/state-
advance behaviour, the row-cap/truncation-note behaviour, issue-text
parsing, the LIKE-pattern query filter, and hostname de-linking in the
HTML body, against a minimal fake psycopg2 connection/cursor - no real
PostgreSQL required.
Run directly:  python3 roles/chrony_watcher/tests/test_chrony_watcher_logic.py
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
            assert like_pattern == "chrony_check[%"
            self._last_rows = [
                r for r in self._rows_by_query["rows"]
                if r["id"] > last_id and r["syslogtag"].startswith("chrony_check[")
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


def make_alert_row(row_id, host="client1.lab.example.com",
                    issue="NTP not synchronized (timedatectl NTPSynchronized=no)"):
    return {
        "id": row_id,
        "message": "chrony_check: %s" % issue,
        "fromhost": host,
        "devicereportedtime": "2026-09-06 12:00:0%d" % (row_id % 10),
        "syslogtag": "chrony_check[%d]:" % (10000 + row_id),
    }


class ChronyWatcherLogicTest(unittest.TestCase):
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

        import chrony_watcher  # noqa: E402
        import importlib
        importlib.reload(chrony_watcher)
        self.watcher = chrony_watcher

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

    def test_second_run_alerts_only_on_new_breaches_and_advances_state(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 42}, fh)

        new_rows = [
            make_alert_row(43, host="client1.lab.example.com",
                           issue="NTP not synchronized (timedatectl NTPSynchronized=no)"),
            make_alert_row(44, host="idm1.lab.example.com",
                           issue="chronyd service not active (systemctl status: failed)"),
        ]
        rows_by_query = {"max_id": 44, "rows": new_rows}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertIn("2 chrony/NTP breach(es) detected across", subject)
        self.assertIn("NTP not synchronized", body)
        self.assertIn("chronyd service not active", body)
        self.assertIn("NTP not synchronized", html_body)
        self.assertIn("chronyd service not active", html_body)
        self.assertIn("<table", html_body)
        # Host column has a zero-width space before every '.' so mail
        # clients don't auto-link the hostname into a clickable
        # http://... URL - renders visually identical to the plain
        # hostname either way.
        self.assertNotIn("client1.lab.example.com", html_body)
        self.assertIn("client1\u200b.lab\u200b.example\u200b.com", html_body)
        self.assertIn("format-detection", html_body)

        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 44)

    def test_single_breach_uses_host_and_issue_in_subject(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        rows_by_query = {
            "max_id": 1,
            "rows": [make_alert_row(
                1, host="idm1.lab.example.com",
                issue="chrony leap status abnormal (Insert second, expected Normal)",
            )],
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
            "[idm1.lab.example.com] chrony/NTP alert: "
            "chrony leap status abnormal (Insert second, expected Normal)",
        )
        self.assertIn("chrony leap status abnormal", body)
        self.assertIn("chrony leap status abnormal", html_body)

    def test_no_new_breaches_sends_nothing_and_state_unchanged(self):
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

        rows = [make_alert_row(i) for i in range(1, 13)]  # 12 rows, cap is 5
        rows_by_query = {"max_id": 12, "rows": rows}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertIn("12 chrony/NTP breach(es)", subject)
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

        matching = [make_alert_row(1)]
        non_matching = [{
            "id": 2, "message": "systemd-logind: some routine notice",
            "fromhost": "client1.lab.example.com",
            "devicereportedtime": "2026-09-06 12:00:02",
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
        self.assertEqual(
            subject,
            "[client1.lab.example.com] chrony/NTP alert: "
            "NTP not synchronized (timedatectl NTPSynchronized=no)",
        )
        # state must still advance to the true max id among matching
        # rows only (1, since row 2 never matched the query filter)
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 1)

    def test_unrelated_message_mentioning_app_name_is_excluded_by_syslogtag(self):
        # Regression test mirroring the real false-positive found in
        # roles/disk_space_watcher's history on 2026-09-06: a sudo
        # audit-log line quoting this app's own message wording inside
        # a logged command must not match a message-text filter.
        # Filtering on syslogtag instead must exclude it.
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        real_breach = make_alert_row(1)
        sudo_audit_line = {
            "id": 2,
            "message": (
                " svc_ansible : TTY=pts/0 ; PWD=/home/svc_ansible/ansible ; "
                "USER=root ; COMMAND=/bin/python3 -c 'chrony_check: NTP not synchronized'"
            ),
            "fromhost": "admin1.lab.example.com",
            "devicereportedtime": "2026-09-06 07:09:47",
            "syslogtag": "sudo:",
        }
        rows_by_query = {"max_id": 2, "rows": [real_breach, sudo_audit_line]}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, _html_body = mock_send.call_args[0]
        self.assertNotIn("sudo", body)
        # state must advance only to the real breach's id (1), since
        # row 2 never matched the syslogtag filter at all
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 1)

    def test_leading_space_message_still_parses_when_syslogtag_matches(self):
        # rsyslog's stored "message" column commonly keeps the single
        # space that follows the "tag:" separator in traditional
        # syslog "tag: msg" framing - parse_alert() strips the message
        # before regex matching, so this must still parse correctly.
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        leading_space_row = {
            "id": 1,
            "message": " chrony_check: NTP not synchronized (timedatectl NTPSynchronized=no)",
            "fromhost": "client1.lab.example.com",
            "devicereportedtime": "2026-09-06 06:43:38",
            "syslogtag": "chrony_check[19846]:",
        }
        rows_by_query = {"max_id": 1, "rows": [leading_space_row]}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertIn("NTP not synchronized", subject)
        self.assertIn("NTP not synchronized", body)
        self.assertIn("NTP not synchronized", html_body)

        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 1)

    def test_parse_alert_falls_back_gracefully_on_unexpected_wording(self):
        self.assertIsNone(
            self.watcher.parse_alert("this does not match the expected format")
        )
        self.assertEqual(
            self.watcher.parse_alert(
                "chrony_check: chronyd service not active (systemctl status: failed)"
            ),
            "chronyd service not active (systemctl status: failed)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
