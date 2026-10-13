#!/usr/bin/env python3
"""
Standalone, throwaway logic test for disk_space_watcher.py - lives in
roles/disk_space_watcher/tests/, NOT roles/disk_space_watcher/files/,
so it is never deployed by Ansible (tasks/main.yml only ever copies
the single named file files/disk_space_watcher.py to a host - nothing
copies this directory at all).

Exercises the bootstrap-without-alerting behaviour, the dedup/state-
advance behaviour, the row-cap/truncation-note behaviour, the mount/
metric/pct/threshold/severity parsing, the LIKE-pattern query filter,
and hostname de-linking in the HTML body, against a minimal fake
psycopg2 connection/cursor - no real PostgreSQL required.
Run directly:  python3 roles/disk_space_watcher/tests/test_disk_space_watcher_logic.py
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
            assert like_pattern == "disk_space_check[%"
            self._last_rows = [
                r for r in self._rows_by_query["rows"]
                if r["id"] > last_id and r["syslogtag"].startswith("disk_space_check[")
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


def make_alert_row(row_id, host="client1.lab.example.com", mount="/var",
                    metric="space", pct=97, threshold=95, severity="CRITICAL"):
    return {
        "id": row_id,
        "message": (
            "disk_space_check: filesystem %s %s usage at %d%% "
            "(threshold %d%%, severity %s)" % (mount, metric, pct, threshold, severity)
        ),
        "fromhost": host,
        "devicereportedtime": "2026-09-05 12:00:0%d" % (row_id % 10),
        "syslogtag": "disk_space_check[%d]:" % (10000 + row_id),
    }


class DiskSpaceWatcherLogicTest(unittest.TestCase):
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

        import disk_space_watcher  # noqa: E402
        import importlib
        importlib.reload(disk_space_watcher)
        self.watcher = disk_space_watcher

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
            make_alert_row(43, mount="/var", metric="space", pct=97, threshold=95, severity="CRITICAL"),
            make_alert_row(44, host="idm1.lab.example.com", mount="/home", metric="inode",
                           pct=88, threshold=85, severity="WARNING"),
        ]
        rows_by_query = {"max_id": 44, "rows": new_rows}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertIn("2 disk/inode threshold breach(es) detected across", subject)
        self.assertIn("CRITICAL", subject)  # worst severity surfaces in the multi-row subject
        self.assertIn("/var", body)
        self.assertIn("/home", body)
        self.assertIn("/var", html_body)
        self.assertIn("/home", html_body)
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

    def test_single_breach_uses_host_mount_and_metric_in_subject(self):
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        rows_by_query = {
            "max_id": 1,
            "rows": [make_alert_row(1, host="idm1.lab.example.com", mount="/var/log",
                                     metric="space", pct=96, threshold=95, severity="CRITICAL")],
        }
        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertEqual(subject, "[idm1.lab.example.com] /var/log space usage 96% (CRITICAL)")
        self.assertIn("/var/log", body)
        self.assertIn(">CRITICAL<", html_body)
        # Regression test for a real formatting bug found 2026-09-06:
        # the HTML table's Usage cell rendered as "96 (threshold 95%)%"
        # (a stray '%' appended after the whole cell, with the pct
        # value itself missing its own '%') instead of the correct
        # "96% (threshold 95%)".
        self.assertIn("96% (threshold 95%)", html_body)
        self.assertNotIn("(threshold 95%)%", html_body)

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
        self.assertIn("12 disk/inode threshold breach(es)", subject)
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
        self.assertEqual(subject, "[client1.lab.example.com] /var space usage 97% (CRITICAL)")
        # state must still advance to the true max id among matching
        # rows only (1, since row 2 never matched the query filter)
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 1)

    def test_leading_space_message_still_parses_when_syslogtag_matches(self):
        # Regression test for a real production finding on 2026-09-06:
        # rsyslog's stored "message" column commonly keeps the single
        # space that follows the "tag:" separator in traditional
        # syslog "tag: msg" framing, e.g. " disk_space_check: filesystem
        # /var space usage at 97% ..." instead of a message with no
        # leading space. The row is now found via its syslogtag (which
        # has no such leading-space quirk), and parse_alert() already
        # strips the message before regex matching, so this must still
        # parse and email correctly end to end.
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        leading_space_row = {
            "id": 1,
            "message": (
                " disk_space_check: filesystem /var space usage at 97% "
                "(threshold 95%, severity CRITICAL)"
            ),
            "fromhost": "client1.lab.example.com",
            "devicereportedtime": "2026-09-06 06:43:38",
            "syslogtag": "disk_space_check[19846]:",
        }
        rows_by_query = {"max_id": 1, "rows": [leading_space_row]}

        with mock.patch.object(
            self.watcher, "get_connection",
            return_value=FakeConn(rows_by_query),
        ), mock.patch.object(self.watcher, "send_email") as mock_send:
            self.watcher.main()

        mock_send.assert_called_once()
        subject, body, html_body = mock_send.call_args[0]
        self.assertEqual(subject, "[client1.lab.example.com] /var space usage 97% (CRITICAL)")
        self.assertIn("/var", body)
        self.assertIn(">CRITICAL<", html_body)

        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 1)

    def test_unrelated_message_mentioning_app_name_is_excluded_by_syslogtag(self):
        # Regression test for a real false-positive found on 2026-09-06:
        # a `sudo` audit-log line for one of this project's own
        # diagnostic commands happened to contain the literal text
        # "disk_space_check: filesystem" inside its logged command
        # line (because the diagnostic command's SQL query text quoted
        # that wording), and would have matched a message-text LIKE
        # filter despite being sudo's own audit trail, not a real
        # breach. Filtering on syslogtag instead must exclude it.
        with open(self.state_file, "w") as fh:
            json.dump({"last_id": 0}, fh)

        real_breach = make_alert_row(1)
        sudo_audit_line = {
            "id": 2,
            "message": (
                " svc_ansible : TTY=pts/0 ; PWD=/home/svc_ansible/ansible ; "
                "USER=root ; COMMAND=/bin/python3 -c 'disk_space_check: filesystem'"
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
        self.assertNotIn("UNKNOWN", subject)
        self.assertNotIn("sudo", body)
        # state must advance only to the real breach's id (1), since
        # row 2 never matched the syslogtag filter at all
        with open(self.state_file) as fh:
            state = json.load(fh)
        self.assertEqual(state["last_id"], 1)

    def test_parse_alert_falls_back_gracefully_on_unexpected_wording(self):
        mount, metric, pct, threshold, severity = self.watcher.parse_alert(
            "this does not match the expected format"
        )
        self.assertIsNone(mount)
        self.assertIsNone(metric)
        self.assertIsNone(pct)
        self.assertIsNone(threshold)
        self.assertIsNone(severity)

        mount, metric, pct, threshold, severity = self.watcher.parse_alert(
            "disk_space_check: filesystem /home inode usage at 88% "
            "(threshold 85%, severity WARNING)"
        )
        self.assertEqual(mount, "/home")
        self.assertEqual(metric, "inode")
        self.assertEqual(pct, "88")
        self.assertEqual(threshold, "85")
        self.assertEqual(severity, "WARNING")


if __name__ == "__main__":
    unittest.main(verbosity=2)
