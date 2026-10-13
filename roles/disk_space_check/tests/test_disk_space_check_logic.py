#!/usr/bin/env python3
"""
Standalone, throwaway logic test for disk_space_check.py - lives in
roles/disk_space_check/tests/, NOT roles/disk_space_check/files/, so
it is never deployed by Ansible (tasks/main.yml only ever copies the
single named file files/disk_space_check.py to a host - nothing copies
this directory at all).

Exercises /proc/mounts parsing (including pseudo-filesystem exclusion
and octal-escape handling), the df-compatible percent-used formulas
for both space and inodes, threshold classification, the end-to-end
check_all() breach list, and main()'s syslog emission (including the
no-alert / nothing-logged case) against a fake statvfs() and a fake
/proc/mounts file - no real filesystem thresholds required.

Run directly:  python3 roles/disk_space_check/tests/test_disk_space_check_logic.py
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

_ROLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROLE_DIR, "files"))


def make_statvfs(f_blocks, f_bfree, f_bavail, f_files=0, f_ffree=0, f_favail=0):
    """A minimal stand-in for os.statvfs_result - only the fields
    disk_space_check.py actually reads need to be present."""
    return types.SimpleNamespace(
        f_blocks=f_blocks,
        f_bfree=f_bfree,
        f_bavail=f_bavail,
        f_files=f_files,
        f_ffree=f_ffree,
        f_favail=f_favail,
    )


class DiskSpaceCheckLogicTest(unittest.TestCase):
    def setUp(self):
        config = types.ModuleType("config")
        config.SPACE_WARN_THRESHOLD = 85
        config.SPACE_CRIT_THRESHOLD = 95
        config.INODE_WARN_THRESHOLD = 85
        config.INODE_CRIT_THRESHOLD = 95
        config.EXCLUDED_FSTYPES = [
            "tmpfs", "devtmpfs", "squashfs", "overlay", "iso9660", "efivarfs",
        ]
        sys.modules["config"] = config

        import disk_space_check  # noqa: E402
        import importlib
        importlib.reload(disk_space_check)
        self.mod = disk_space_check

    # -- /proc/mounts parsing -------------------------------------------------

    def _write_proc_mounts(self, content):
        fh = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".mounts")
        fh.write(content)
        fh.close()
        return fh.name

    def test_iter_real_mounts_excludes_pseudo_filesystems(self):
        content = (
            "/dev/sda1 / xfs rw,relatime 0 0\n"
            "tmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0\n"
            "/dev/sda2 /var xfs rw,relatime 0 0\n"
            "overlay /var/lib/containers/overlay overlay rw 0 0\n"
        )
        path = self._write_proc_mounts(content)
        try:
            with mock.patch.object(self.mod, "open", mock.mock_open(read_data=content)):
                mounts = self.mod.iter_real_mounts()
        finally:
            os.remove(path)
        self.assertEqual(mounts, [("/", "xfs"), ("/var", "xfs")])

    def test_iter_real_mounts_unescapes_octal_sequences(self):
        content = "/dev/sdb1 /mnt/my\\040data xfs rw,relatime 0 0\n"
        with mock.patch.object(self.mod, "open", mock.mock_open(read_data=content)):
            mounts = self.mod.iter_real_mounts()
        self.assertEqual(mounts, [("/mnt/my data", "xfs")])

    def test_iter_real_mounts_dedupes_keeping_last_entry(self):
        content = (
            "/dev/sda1 / xfs rw 0 0\n"
            "/dev/sda1 / xfs ro,remount 0 0\n"
        )
        with mock.patch.object(self.mod, "open", mock.mock_open(read_data=content)):
            mounts = self.mod.iter_real_mounts()
        self.assertEqual(mounts, [("/", "xfs")])

    # -- percent-used formulas -------------------------------------------------

    def test_usage_percentages_matches_df_rounding(self):
        # used=950, avail=50 -> 950/1000 = 95% exactly
        st = make_statvfs(f_blocks=1000, f_bfree=50, f_bavail=50, f_files=100, f_ffree=20, f_favail=20)
        with mock.patch.object(self.mod.os, "statvfs", return_value=st):
            space_pct, inode_pct = self.mod.usage_percentages("/var")
        self.assertEqual(space_pct, 95)
        self.assertEqual(inode_pct, 80)

    def test_usage_percentages_rounds_up_on_fraction(self):
        # used=1, avail=99 among 100 usable slots -> exactly 1%, but
        # used=1, avail=998 (999 usable) -> 1/999*100 = 0.1001...% must
        # round UP to 1%, never down to 0%, matching df's own behaviour.
        st = make_statvfs(f_blocks=1000, f_bfree=999, f_bavail=998, f_files=0, f_ffree=0, f_favail=0)
        with mock.patch.object(self.mod.os, "statvfs", return_value=st):
            space_pct, inode_pct = self.mod.usage_percentages("/")
        self.assertEqual(space_pct, 1)
        self.assertIsNone(inode_pct)  # f_files == 0 -> no inode data reported

    def test_usage_percentages_zero_total_inodes_reports_none(self):
        st = make_statvfs(f_blocks=100, f_bfree=50, f_bavail=50, f_files=0, f_ffree=0, f_favail=0)
        with mock.patch.object(self.mod.os, "statvfs", return_value=st):
            _space_pct, inode_pct = self.mod.usage_percentages("/some/nfs/mount")
        self.assertIsNone(inode_pct)

    # -- classify() --------------------------------------------------------

    def test_classify_boundaries(self):
        self.assertIsNone(self.mod.classify(84, 85, 95))
        self.assertEqual(self.mod.classify(85, 85, 95), "WARNING")
        self.assertEqual(self.mod.classify(94, 85, 95), "WARNING")
        self.assertEqual(self.mod.classify(95, 85, 95), "CRITICAL")
        self.assertEqual(self.mod.classify(100, 85, 95), "CRITICAL")
        self.assertIsNone(self.mod.classify(None, 85, 95))

    # -- check_all() end-to-end --------------------------------------------

    def test_check_all_reports_both_metrics_when_both_breach(self):
        content = "/dev/sda2 /var xfs rw 0 0\n"
        st = make_statvfs(f_blocks=1000, f_bfree=40, f_bavail=40, f_files=1000, f_ffree=30, f_favail=30)
        with mock.patch.object(self.mod, "open", mock.mock_open(read_data=content)), \
                mock.patch.object(self.mod.os, "statvfs", return_value=st):
            alerts = self.mod.check_all()
        self.assertEqual(len(alerts), 2)
        metrics = {a[1] for a in alerts}
        self.assertEqual(metrics, {"space", "inode"})
        for mount, _metric, _pct, _threshold, severity in alerts:
            self.assertEqual(mount, "/var")
            self.assertEqual(severity, "CRITICAL")

    def test_check_all_reports_nothing_when_under_threshold(self):
        content = "/dev/sda1 / xfs rw 0 0\n"
        st = make_statvfs(f_blocks=1000, f_bfree=500, f_bavail=500, f_files=1000, f_ffree=500, f_favail=500)
        with mock.patch.object(self.mod, "open", mock.mock_open(read_data=content)), \
                mock.patch.object(self.mod.os, "statvfs", return_value=st):
            alerts = self.mod.check_all()
        self.assertEqual(alerts, [])

    def test_check_all_skips_mount_that_raises_oserror(self):
        content = "/dev/sda1 / xfs rw 0 0\n/dev/sdb1 /mnt/hung nfs4 rw 0 0\n"
        with mock.patch.object(self.mod, "open", mock.mock_open(read_data=content)), \
                mock.patch.object(self.mod.os, "statvfs", side_effect=OSError("stale handle")):
            alerts = self.mod.check_all()
        self.assertEqual(alerts, [])  # both mounts errored, neither crashes the run

    # -- format_message() ----------------------------------------------------

    def test_format_message_wording(self):
        msg = self.mod.format_message("/var", "space", 97, 95, "CRITICAL")
        self.assertEqual(
            msg,
            "disk_space_check: filesystem /var space usage at 97% "
            "(threshold 95%, severity CRITICAL)",
        )

    # -- main() / syslog emission --------------------------------------------

    def test_main_emits_no_syslog_calls_when_nothing_breaches(self):
        with mock.patch.object(self.mod, "check_all", return_value=[]), \
                mock.patch.object(self.mod, "syslog") as mock_syslog:
            self.mod.main()
        mock_syslog.openlog.assert_not_called()
        mock_syslog.syslog.assert_not_called()

    def test_main_emits_one_syslog_call_per_breach_with_correct_priority(self):
        alerts = [
            ("/var", "space", 97, 95, "CRITICAL"),
            ("/home", "inode", 88, 85, "WARNING"),
        ]
        with mock.patch.object(self.mod, "check_all", return_value=alerts), \
                mock.patch.object(self.mod, "syslog") as mock_syslog:
            mock_syslog.LOG_WARNING = "WARN_PRIO"
            mock_syslog.LOG_CRIT = "CRIT_PRIO"
            mock_syslog.LOG_PID = 1
            mock_syslog.LOG_DAEMON = 3
            self.mod._SYSLOG_PRIORITY = {"WARNING": "WARN_PRIO", "CRITICAL": "CRIT_PRIO"}
            self.mod.main()

        mock_syslog.openlog.assert_called_once()
        self.assertEqual(mock_syslog.syslog.call_count, 2)
        first_call, second_call = mock_syslog.syslog.call_args_list
        self.assertEqual(first_call.args[0], "CRIT_PRIO")
        self.assertIn("filesystem /var space usage at 97%", first_call.args[1])
        self.assertEqual(second_call.args[0], "WARN_PRIO")
        self.assertIn("filesystem /home inode usage at 88%", second_call.args[1])
        mock_syslog.closelog.assert_called_once()


if __name__ == "__main__":
    unittest.main()
