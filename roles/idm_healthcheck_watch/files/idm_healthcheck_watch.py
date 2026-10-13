#!/usr/bin/env python3
"""
idm_healthcheck_watch.py - runs `ipa-healthcheck --output-type=json`
locally on THIS IdM server and logs one syslog message per non-SUCCESS
finding - see roles/idm_healthcheck_watch/README.md.

Pure standard library (subprocess + syslog + json) - no third-party
packages. Deliberately does NOT email anything itself and has no local
mail relay dependency: it only ever logs to syslog, which
roles/rsyslog_client's existing imjournal forwarding (already enabled
fleet-wide) carries to the collector like any other journald line.
roles/idm_healthcheck_watcher, on the collector only, polls log_db for
these messages and sends the actual alert email - see that role's
README.md for the other half of this mechanism. Same architecture as
roles/chrony_check/roles/disk_space_check.

Deliberately separate from roles/idm_healthchecks (an ad hoc/post-
patch Ansible task) - this script is invoked directly by systemd on a
timer, unattended, and never touches Ansible at all.

Runs as root (see roles/idm_healthcheck_watch/README.md "Why this runs
as root") - ipa-healthcheck itself requires root to inspect 389-ds/
certmonger/replication internals.
"""
import json
import subprocess
import syslog

import config

# Fixed wording this script alone emits and roles/idm_healthcheck_watcher
# alone parses - analogous to roles/disk_space_check's own fixed alert
# wording. Keeping hostname OUT of the message text (the collector
# gets that from the syslog fromhost column already) matches that same
# precedent.
_ISSUE_MESSAGE_FMT = "idm_healthcheck_watch: %s.%s %s: %s"

# A second, distinct message shape for when ipa-healthcheck itself
# couldn't be run or its output couldn't be parsed at all (missing
# binary, IPA not running, timeout, malformed JSON, ...) - this is
# itself worth an alert (silence from this host could otherwise be
# mistaken for "all healthy"), so it is logged at CRITICAL rather than
# just dropped. roles/idm_healthcheck_watcher recognises this shape
# too - see that role's files/idm_healthcheck_watcher.py.
_RUN_FAILED_MESSAGE_FMT = "idm_healthcheck_watch: RUN_FAILED: %s"

# Results ipa-healthcheck itself may emit ("PASS" is not documented as
# an actual ipa-healthcheck value today, but is ignored defensively
# alongside "SUCCESS" in case a future/plugin check ever emits it).
_IGNORED_RESULTS = ("SUCCESS", "PASS")

# Only WARNING/ERROR/CRITICAL are meaningful syslog priorities here;
# anything else (an unrecognised future result value) is still logged,
# at LOG_WARNING, so it is never silently dropped.
_SYSLOG_PRIORITY = {
    "WARNING": syslog.LOG_WARNING,
    "ERROR": syslog.LOG_CRIT,
    "CRITICAL": syslog.LOG_CRIT,
}


def _build_command():
    cmd = ["ipa-healthcheck", "--output-type=json"]
    if config.MODE == "services":
        cmd.append("--source=ipahealthcheck.meta.services")
    elif config.MODE == "replication_topology":
        cmd.append("--source=ipahealthcheck.ds.replication")
        cmd.append("--source=ipahealthcheck.ipa.topology")
    # "all" (the default) passes no --source at all, matching
    # roles/idm_healthchecks' own idm_hc_mode == "all" behaviour.
    for source in config.EXTRA_SOURCES:
        cmd.append("--source=%s" % source)
    return cmd


def run_healthcheck():
    """Returns (parsed_list, run_failed_reason). Exactly one of the
    two is falsy/None - a run either yields a parsed list (possibly
    empty, meaning "no issues") or a failure reason, never both.

    Deliberately does NOT treat a non-zero exit code as failure:
    ipa-healthcheck exits non-zero whenever it finds WARNING/ERROR/
    CRITICAL issues - that is its normal, documented behaviour, not a
    run failure. Only an unparsable/empty result is a run failure.
    """
    try:
        proc = subprocess.run(
            _build_command(),
            # stdout/stderr=PIPE + universal_newlines rather than
            # capture_output/text - those kwargs need Python 3.7+, and
            # this script must also run on RHEL 8's python 3.6.
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=config.TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "ipa-healthcheck timed out after %ds" % config.TIMEOUT_SECONDS
    except OSError as exc:
        return None, "could not run ipa-healthcheck: %s" % exc

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None, "ipa-healthcheck produced no output (exit code %s)" % proc.returncode

    try:
        parsed = json.loads(stdout)
    except ValueError as exc:
        return None, "could not parse ipa-healthcheck JSON output: %s" % exc

    if not isinstance(parsed, list):
        return None, "unexpected ipa-healthcheck JSON shape (expected a list)"

    return parsed, None


def log_issue(entry):
    result = str(entry.get("result", "")).upper()
    if result in _IGNORED_RESULTS:
        return
    source = entry.get("source", "unknown_source")
    check = entry.get("check", "unknown_check")
    msg = (entry.get("kw") or {}).get("msg", "no message provided")
    priority = _SYSLOG_PRIORITY.get(result, syslog.LOG_WARNING)
    syslog.syslog(priority, _ISSUE_MESSAGE_FMT % (source, check, result, msg))


def main():
    syslog.openlog(
        ident="idm_healthcheck_watch",
        logoption=syslog.LOG_PID,
        facility=syslog.LOG_DAEMON,
    )
    try:
        parsed, run_failed_reason = run_healthcheck()
        if run_failed_reason is not None:
            syslog.syslog(syslog.LOG_CRIT, _RUN_FAILED_MESSAGE_FMT % run_failed_reason)
            return
        for entry in parsed:
            if isinstance(entry, dict):
                log_issue(entry)
    finally:
        syslog.closelog()


if __name__ == "__main__":
    main()
