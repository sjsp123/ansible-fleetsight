#!/usr/bin/env python3
"""
disk_space_check.py - walks every real (non-pseudo) mounted filesystem
on THIS host and logs one syslog message per breach of the configured
disk-space or inode utilisation thresholds - see
roles/disk_space_check/README.md.

Pure standard library (os.statvfs + /proc/mounts + syslog) - no
subprocess calls to df/awk, no third-party packages. Deliberately does
NOT email anything itself and has no local mail relay dependency: it
only ever logs to syslog, which roles/rsyslog_client's existing
imjournal forwarding (already enabled fleet-wide) carries to the
collector like any other journald line. roles/disk_space_watcher, on
the collector only, polls log_db for these messages and sends the
actual alert email - see that role's README.md for the other half of
this mechanism.

Run on a short systemd timer (roles/disk_space_check/templates/
disk-space-check.timer.j2), not continuously - each run is a single
check-and-exit cycle. Deployed to every [log_clients] host, including
the collector itself (it is also a log_clients member), so the
collector's own log_db growth is covered by the same mechanism as
every other host.

Thresholds and the percent-used formulas below intentionally mirror
roles/linuxdb_reports/files/df_check.sh (WARN=85%/CRIT=95% by default,
the same excluded pseudo-filesystem types) so the numbers a human sees
here line up with what the dashboard already shows - see that script's
comments for the reasoning behind the specific threshold values and
exclusions.
"""
import os
import syslog

import config

_EXCLUDED_TYPES = frozenset(config.EXCLUDED_FSTYPES)

# Fixed wording this script alone emits and roles/disk_space_watcher
# alone parses - analogous to systemd's own fixed "Failed with result"
# wording that roles/service_failure_watcher matches on. Keeping
# hostname OUT of the message text (the collector gets that from the
# syslog fromhost column already) matches that same precedent.
_MESSAGE_FMT = (
    "disk_space_check: filesystem %s %s usage at %d%% "
    "(threshold %d%%, severity %s)"
)

_SYSLOG_PRIORITY = {
    "WARNING": syslog.LOG_WARNING,
    "CRITICAL": syslog.LOG_CRIT,
}


def iter_real_mounts():
    """Parses /proc/mounts, yielding (mountpoint, fstype) for every
    real (non-pseudo) filesystem, deduplicated by mountpoint (the same
    target can appear more than once for bind mounts/remounts - only
    the last entry, the one actually in effect, is kept). Sorted for
    stable, predictable ordering across runs."""
    mounts = {}
    with open("/proc/mounts", "r", encoding="utf-8") as fh:
        for line in fh:
            fields = line.split()
            if len(fields) < 3:
                continue
            mountpoint, fstype = fields[1], fields[2]
            if fstype in _EXCLUDED_TYPES:
                continue
            # /proc/mounts octal-escapes spaces/tabs/backslashes in
            # mountpoint paths - unescape the common ones.
            mountpoint = (
                mountpoint.replace("\\040", " ")
                .replace("\\011", "\t")
                .replace("\\134", "\\")
            )
            mounts[mountpoint] = fstype
    return sorted(mounts.items())


def _pct_used(used, avail):
    """Percent used, rounded UP - matches GNU df's own rounding, which
    never under-reports how close a filesystem is to full. Returns 0
    for a degenerate filesystem that reports no capacity at all."""
    denom = used + avail
    if denom <= 0:
        return 0
    return -(-used * 100 // denom)


def usage_percentages(mountpoint):
    """Returns (space_pct, inode_pct) for one mountpoint, computed the
    same way `df`/`df -i` do: used / (used + available-to-non-root) *
    100. inode_pct is None when the filesystem reports zero total
    inodes (some network/pseudo filesystems do), so callers can skip
    the inode check for those rather than reporting a meaningless 0%."""
    st = os.statvfs(mountpoint)

    space_pct = _pct_used(st.f_blocks - st.f_bfree, st.f_bavail)

    inode_pct = None
    if st.f_files > 0:
        inode_pct = _pct_used(st.f_files - st.f_ffree, st.f_favail)

    return space_pct, inode_pct


def classify(pct, warn_threshold, crit_threshold):
    """Returns 'CRITICAL', 'WARNING', or None. None input (no inode
    data for this filesystem) always returns None."""
    if pct is None:
        return None
    if pct >= crit_threshold:
        return "CRITICAL"
    if pct >= warn_threshold:
        return "WARNING"
    return None


def check_all():
    """Returns a list of (mountpoint, metric, pct, threshold, severity)
    tuples, one per breach found across every real mounted filesystem."""
    alerts = []
    for mountpoint, _fstype in iter_real_mounts():
        try:
            space_pct, inode_pct = usage_percentages(mountpoint)
        except OSError:
            # Mountpoint vanished or is unreachable (unmounted mid-run,
            # a hung NFS mount, etc.) between listing and statvfs() -
            # skip it rather than aborting the whole run over one
            # filesystem; it'll be picked up again next poll if it's
            # still a problem.
            continue

        space_sev = classify(
            space_pct, config.SPACE_WARN_THRESHOLD, config.SPACE_CRIT_THRESHOLD
        )
        if space_sev:
            threshold = (
                config.SPACE_CRIT_THRESHOLD
                if space_sev == "CRITICAL"
                else config.SPACE_WARN_THRESHOLD
            )
            alerts.append((mountpoint, "space", space_pct, threshold, space_sev))

        inode_sev = classify(
            inode_pct, config.INODE_WARN_THRESHOLD, config.INODE_CRIT_THRESHOLD
        )
        if inode_sev:
            threshold = (
                config.INODE_CRIT_THRESHOLD
                if inode_sev == "CRITICAL"
                else config.INODE_WARN_THRESHOLD
            )
            alerts.append((mountpoint, "inode", inode_pct, threshold, inode_sev))

    return alerts


def format_message(mountpoint, metric, pct, threshold, severity):
    return _MESSAGE_FMT % (mountpoint, metric, pct, threshold, severity)


def main():
    alerts = check_all()
    if not alerts:
        return

    syslog.openlog(
        ident="disk_space_check", logoption=syslog.LOG_PID, facility=syslog.LOG_DAEMON
    )
    try:
        for mountpoint, metric, pct, threshold, severity in alerts:
            syslog.syslog(
                _SYSLOG_PRIORITY[severity],
                format_message(mountpoint, metric, pct, threshold, severity),
            )
    finally:
        syslog.closelog()


if __name__ == "__main__":
    main()
