#!/usr/bin/env python3
"""
Standalone, throwaway logic test for idm_healthcheck_watch.py - lives
in roles/idm_healthcheck_watch/tests/, NOT roles/idm_healthcheck_watch/
files/, so it is never deployed by Ansible (tasks/main.yml only ever
copies the single named file files/idm_healthcheck_watch.py to a
host - nothing copies this directory at all).

Exercises command-building for all three idm_hc-style modes, the
non-zero-exit-code-is-not-a-failure distinction, the three run-failure
paths (timeout, missing/OSError, empty output, unparsable JSON,
wrong JSON shape), SUCCESS/PASS filtering, and the syslog priority
mapping and message wording for WARNING/ERROR/CRITICAL findings -
against a fake subprocess.run() and a fake syslog module. No real
ipa-healthcheck binary or root privilege required.

Run directly:  python3 roles/idm_healthcheck_watch/tests/test_idm_healthcheck_watch_logic.py
"""
import json
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

_ROLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROLE_DIR, "files"))


def make_proc(stdout="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode)


class IdmHealthcheckWatchLogicTest(unittest.TestCase):
    def setUp(self):
        config = types.ModuleType("config")
        config.MODE = "all"
        config.EXTRA_SOURCES = []
        config.TIMEOUT_SECONDS = 180
        sys.modules["config"] = config

        import idm_healthcheck_watch  # noqa: E402
        import importlib
        importlib.reload(idm_healthcheck_watch)
        self.mod = idm_healthcheck_watch

    # -- command building -----------------------------------------------

    def test_build_command_all_mode_has_no_source_flags(self):
        cmd = self.mod._build_command()
        self.assertEqual(cmd, ["ipa-healthcheck", "--output-type=json"])

    def test_build_command_services_mode(self):
        self.mod.config.MODE = "services"
        cmd = self.mod._build_command()
        self.assertIn("--source=ipahealthcheck.meta.services", cmd)

    def test_build_command_replication_topology_mode(self):
        self.mod.config.MODE = "replication_topology"
        cmd = self.mod._build_command()
        self.assertIn("--source=ipahealthcheck.ds.replication", cmd)
        self.assertIn("--source=ipahealthcheck.ipa.topology", cmd)

    def test_build_command_appends_extra_sources(self):
        self.mod.config.EXTRA_SOURCES = ["ipahealthcheck.dogtag.ca"]
        cmd = self.mod._build_command()
        self.assertIn("--source=ipahealthcheck.dogtag.ca", cmd)

    # -- run_healthcheck() ------------------------------------------------

    def test_nonzero_exit_with_valid_json_is_not_a_run_failure(self):
        # ipa-healthcheck's own documented behaviour: exits non-zero
        # whenever it finds WARNING/ERROR/CRITICAL issues - that must
        # never be conflated with "the run itself failed".
        sample = [{"source": "s", "check": "c", "result": "CRITICAL", "kw": {"msg": "bad"}}]
        with mock.patch.object(subprocess, "run", return_value=make_proc(json.dumps(sample), returncode=1)):
            parsed, failed = self.mod.run_healthcheck()
        self.assertIsNone(failed)
        self.assertEqual(parsed, sample)

    def test_timeout_is_a_run_failure(self):
        with mock.patch.object(
            subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="ipa-healthcheck", timeout=180),
        ):
            parsed, failed = self.mod.run_healthcheck()
        self.assertIsNone(parsed)
        self.assertIn("timed out", failed)

    def test_missing_binary_is_a_run_failure(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("not found")):
            parsed, failed = self.mod.run_healthcheck()
        self.assertIsNone(parsed)
        self.assertIn("could not run ipa-healthcheck", failed)

    def test_empty_output_is_a_run_failure(self):
        with mock.patch.object(subprocess, "run", return_value=make_proc("", returncode=1)):
            parsed, failed = self.mod.run_healthcheck()
        self.assertIsNone(parsed)
        self.assertIn("no output", failed)

    def test_unparsable_json_is_a_run_failure(self):
        with mock.patch.object(subprocess, "run", return_value=make_proc("not json", returncode=0)):
            parsed, failed = self.mod.run_healthcheck()
        self.assertIsNone(parsed)
        self.assertIn("could not parse", failed)

    def test_wrong_json_shape_is_a_run_failure(self):
        with mock.patch.object(subprocess, "run", return_value=make_proc(json.dumps({"not": "a list"}))):
            parsed, failed = self.mod.run_healthcheck()
        self.assertIsNone(parsed)
        self.assertIn("expected a list", failed)

    # -- log_issue() / syslog priority mapping -----------------------------

    def test_success_and_pass_are_never_logged(self):
        logged = []
        with mock.patch.object(self.mod.syslog, "syslog", side_effect=lambda p, m: logged.append((p, m))):
            self.mod.log_issue({"source": "s", "check": "c", "result": "SUCCESS", "kw": {"msg": "ok"}})
            self.mod.log_issue({"source": "s", "check": "c", "result": "PASS", "kw": {"msg": "ok"}})
        self.assertEqual(logged, [])

    def test_warning_logged_at_log_warning_with_expected_wording(self):
        logged = []
        with mock.patch.object(self.mod.syslog, "syslog", side_effect=lambda p, m: logged.append((p, m))):
            self.mod.log_issue({
                "source": "ipahealthcheck.meta.services", "check": "ServiceCheck",
                "result": "WARNING", "kw": {"msg": "service degraded"},
            })
        self.assertEqual(len(logged), 1)
        priority, message = logged[0]
        self.assertEqual(priority, self.mod.syslog.LOG_WARNING)
        self.assertEqual(
            message,
            "idm_healthcheck_watch: ipahealthcheck.meta.services.ServiceCheck WARNING: service degraded",
        )

    def test_error_and_critical_both_logged_at_log_crit(self):
        logged = []
        with mock.patch.object(self.mod.syslog, "syslog", side_effect=lambda p, m: logged.append((p, m))):
            self.mod.log_issue({"source": "s", "check": "c1", "result": "ERROR", "kw": {"msg": "e"}})
            self.mod.log_issue({"source": "s", "check": "c2", "result": "CRITICAL", "kw": {"msg": "c"}})
        self.assertEqual(logged[0][0], self.mod.syslog.LOG_CRIT)
        self.assertEqual(logged[1][0], self.mod.syslog.LOG_CRIT)

    def test_missing_msg_falls_back_gracefully(self):
        logged = []
        with mock.patch.object(self.mod.syslog, "syslog", side_effect=lambda p, m: logged.append((p, m))):
            self.mod.log_issue({"source": "s", "check": "c", "result": "WARNING"})
        self.assertIn("no message provided", logged[0][1])

    # -- main() end to end --------------------------------------------------

    def test_main_logs_run_failed_at_log_crit_when_run_fails(self):
        logged = []
        with mock.patch.object(subprocess, "run", side_effect=OSError("no such file")), \
             mock.patch.object(self.mod.syslog, "syslog", side_effect=lambda p, m: logged.append((p, m))), \
             mock.patch.object(self.mod.syslog, "openlog"), \
             mock.patch.object(self.mod.syslog, "closelog"):
            self.mod.main()
        self.assertEqual(len(logged), 1)
        priority, message = logged[0]
        self.assertEqual(priority, self.mod.syslog.LOG_CRIT)
        self.assertTrue(message.startswith("idm_healthcheck_watch: RUN_FAILED: "))

    def test_main_logs_nothing_when_all_results_are_success(self):
        logged = []
        sample = [{"source": "s", "check": "c", "result": "SUCCESS", "kw": {"msg": "ok"}}]
        with mock.patch.object(subprocess, "run", return_value=make_proc(json.dumps(sample))), \
             mock.patch.object(self.mod.syslog, "syslog", side_effect=lambda p, m: logged.append((p, m))), \
             mock.patch.object(self.mod.syslog, "openlog"), \
             mock.patch.object(self.mod.syslog, "closelog"):
            self.mod.main()
        self.assertEqual(logged, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
