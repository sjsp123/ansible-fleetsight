#!/usr/bin/env python3
"""
chrony_check.py - inspects THIS host's NTP/chrony status and logs one
syslog message per breach of the expected state - see
roles/chrony_check/README.md.

Pure standard library (subprocess + syslog) - no third-party packages.
Deliberately does NOT email anything itself and has no local mail
relay dependency: it only ever logs to syslog, which
roles/rsyslog_client's existing imjournal forwarding (already enabled
fleet-wide) carries to the collector like any other journald line.
roles/chrony_watcher, on the collector only, polls log_db for these
messages and sends the actual alert email - see that role's README.md
for the other half of this mechanism.

Run on a short systemd timer (roles/chrony_check/templates/
chrony-check.timer.j2), not continuously - each run is a single
check-and-exit cycle. Deployed to every [log_clients] host, including
the collector itself (it is also a log_clients member).

Three independent conditions are checked, mirroring exactly what
roles/linuxdb_dashboard already flags as a WARNING on the fleet
dashboard (see group_vars/all/host_checks.yml's time_sync/
chronyd_status/chrony_leap checks and
roles/linuxdb_dashboard/files/app.py's NTP_SYNC/CHRONYD flagging
logic), so what triggers an email here lines up with what the
dashboard already shows:

1. NTP not synchronized - `timedatectl show -p NTPSynchronized --value`
   returns anything other than "yes".
2. chronyd not active - `systemctl is-active chronyd` returns anything
   other than "active".
3. Chrony leap status abnormal - `chronyc tracking`'s "Leap status"
   line is anything other than "Normal".

Every check is read-only (no elevated privilege, no state changed on
the host) and shells out to a fixed argv list only - never a shell
string, so there is no command-injection surface at all.
"""
import subprocess
import syslog

# Fixed wording this script alone emits and roles/chrony_watcher alone
# parses - analogous to roles/disk_space_check's own fixed alert
# wording. Keeping hostname OUT of the message text (the collector
# gets that from the syslog fromhost column already) matches that same
# precedent.
_MESSAGE_FMT = "chrony_check: %s"

_TIMEOUT_SECONDS = 10


def _run(argv):
    """Runs a fixed argv list and returns (rc, stdout_stripped). Never
    raises on a non-zero exit or on the command being missing/timing
    out - callers treat those as "unknown" state rather than crashing
    the whole check run over one unavailable command."""
    try:
        proc = subprocess.run(
            argv,
            # stdout/stderr=PIPE + universal_newlines rather than
            # capture_output/text - those kwargs need Python 3.7+, and
            # this script must also run on RHEL 8's python 3.6.
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
        return proc.returncode, proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, ""


def check_ntp_synchronized():
    """Returns an issue string, or None if NTP is synchronized (or the
    command couldn't be run at all, i.e. unknown - never alerted on)."""
    rc, out = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if rc is None:
        return None
    if out == "no":
        return "NTP not synchronized (timedatectl NTPSynchronized=no)"
    return None


def check_chronyd_active():
    """Returns an issue string, or None if chronyd is active (or the
    command couldn't be run at all)."""
    rc, out = _run(["systemctl", "is-active", "chronyd"])
    if rc is None:
        return None
    status = out or "unknown"
    if status != "active":
        return "chronyd service not active (systemctl status: %s)" % status
    return None


def parse_leap_status(tracking_output):
    """Extracts the value of chronyc tracking's "Leap status" line,
    e.g. "Leap status     : Normal" -> "Normal". Returns None if the
    line isn't present at all (unexpected chronyc output format)."""
    for line in tracking_output.splitlines():
        if line.strip().startswith("Leap status"):
            _label, _sep, value = line.partition(":")
            return value.strip()
    return None


def check_chrony_leap():
    """Returns an issue string, or None if leap status is Normal (or
    the command couldn't be run / the line couldn't be found at all,
    i.e. unknown - never alerted on)."""
    rc, out = _run(["chronyc", "tracking"])
    if rc is None or rc != 0:
        return None
    leap = parse_leap_status(out)
    if leap is None:
        return None
    if leap != "Normal":
        return "chrony leap status abnormal (%s, expected Normal)" % leap
    return None


def check_all():
    """Returns a list of issue strings, one per breach found (empty
    list when everything is healthy)."""
    issues = []
    for check in (check_ntp_synchronized, check_chronyd_active, check_chrony_leap):
        issue = check()
        if issue:
            issues.append(issue)
    return issues


def format_message(issue):
    return _MESSAGE_FMT % issue


def main():
    issues = check_all()
    if not issues:
        return

    syslog.openlog(
        ident="chrony_check", logoption=syslog.LOG_PID, facility=syslog.LOG_DAEMON
    )
    try:
        for issue in issues:
            syslog.syslog(syslog.LOG_WARNING, format_message(issue))
    finally:
        syslog.closelog()


if __name__ == "__main__":
    main()
