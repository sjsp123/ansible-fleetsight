"""Exercise the measurement script without accessing any real host/database."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "collect_storage_sizing.sh"
MOCK = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
with open(os.environ["CALL_LOG"], "a") as log:
    log.write(json.dumps([name, sys.argv[1:]]) + "\n")
if name == "sudo":
    args = sys.argv[1:]
    if args[:2] == ["-u", "postgres"]:
        args = args[2:]
    os.execvp(args[0], args)
if name == "psql":
    Path(os.environ["SQL_CAPTURE"]).write_text(sys.stdin.read())
    Path(os.environ["OPTIONS_CAPTURE"]).write_text(os.environ.get("PGOPTIONS", ""))
if name == os.environ.get("FAIL_COMMAND"):
    print("simulated failure", file=sys.stderr)
    sys.exit(1)
if name == "find":
    print(".\n.\n.")
else:
    print(name + ": mock measurement")
"""


class StorageMeasurementTests(unittest.TestCase):
    def invoke(self, *args, fail=""):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for command in ("sudo", "date", "df", "vgs", "du", "find", "psql"):
                path = root / command
                path.write_text(MOCK)
                path.chmod(0o755)
            env = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                CALL_LOG=str(root / "calls"),
                SQL_CAPTURE=str(root / "sql"),
                OPTIONS_CAPTURE=str(root / "options"),
                FAIL_COMMAND=fail,
            )
            result = subprocess.run(
                ["bash", str(SCRIPT), *args],
                env=env, text=True, capture_output=True, timeout=15,
            )
            values = [
                (root / name).read_text() if (root / name).exists() else ""
                for name in ("calls", "sql", "options")
            ]
            return result, *values

    def test_bash_syntax(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_help_does_not_measure(self):
        result, calls, _, _ = self.invoke("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)
        self.assertEqual(calls, "")

    def test_unknown_argument_does_not_measure(self):
        result, calls, _, _ = self.invoke("--apply")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, "")

    def test_success_readonly_options_and_sql(self):
        result, calls, sql, options = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("All checks completed", result.stdout)
        self.assertIn('"--readonly"', calls)
        self.assertIn('"ON_ERROR_STOP=1"', calls)
        self.assertIn("default_transaction_read_only=on", options)
        self.assertIn("statement_timeout=60000", options)
        self.assertIn("lock_timeout=3000", options)
        self.assertEqual(sql.count("SELECT"), 4)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        self.assertTrue(all(s.startswith(("SHOW ", "SELECT")) for s in statements))
        self.assertNotIn("SELECT *", sql)

    def test_directory_failure_still_collects_database(self):
        result, _, sql, _ = self.invoke(fail="du")
        self.assertEqual(result.returncode, 1)
        self.assertIn("INCOMPLETE: 1", result.stderr)
        self.assertIn("SELECT", sql)

    def test_find_failure_not_hidden_by_wc(self):
        result, _, sql, _ = self.invoke(fail="find")
        self.assertEqual(result.returncode, 1)
        self.assertIn("INCOMPLETE: 1", result.stderr)
        self.assertIn("SELECT", sql)

    def test_database_failure_preserves_earlier_measurements(self):
        result, _, _, _ = self.invoke(fail="psql")
        self.assertEqual(result.returncode, 1)
        self.assertIn("df: mock measurement", result.stdout)
        self.assertIn("INCOMPLETE: 1", result.stderr)


if __name__ == "__main__":
    unittest.main()
