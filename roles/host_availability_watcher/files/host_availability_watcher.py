#!/usr/bin/env python3
"""
host_availability_watcher.py - actively polls every host in
config.HOSTS from THIS collector, via ICMP ping and a TCP connect to
config.SSH_PORT, and emails a batched summary whenever a host's
availability STATE changes - see
roles/host_availability_watcher/README.md.

Unlike roles/service_failure_watcher and roles/log_alerter (both of
which are entirely passive - they only notice a problem if the
affected host is still alive enough to forward a log line about it to
the collector), this role is an active heartbeat: it directly checks
reachability from the collector, so it is the only one of the three
that can detect a host that is completely unreachable (powered off,
network partition, kernel panic, hard hang, etc.) - the one scenario
the other two can never see, since a dead host cannot log anything
about its own death.

Run on a short systemd timer (roles/host_availability_watcher/
templates/host-availability-watcher.timer.j2), not continuously - each
run is a single poll-and-exit cycle across every configured host.
Per-host state (consecutive failure counts, current state, and the
timestamp the current state began) is kept in a small JSON file at
config.STATE_FILE between runs, so a single blip does not trigger an
alert and a state change is only emailed once - not on every poll
while it persists.

Pure standard library - no third-party dependencies at all (uses
subprocess for `ping` and the socket module for the SSH TCP check) -
matching the "pure stdlib" precedent already set by log_search_ui/
linuxdb_dashboard/log_alerter/service_failure_watcher.
"""
import html
import json
import os
import smtplib
import syslog
import socket
import subprocess
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

import config

# Shared inline styles for the HTML table part of the alert email -
# inline (not a <style> block) because most mail clients strip
# <head>/<style> content and only reliably honour inline style=
# attributes.
_TH_STYLE = (
    "padding:4px 10px;border:1px solid #ccc;background:#f0f0f0;"
    "text-align:left;font-family:Arial,Helvetica,sans-serif;font-size:13px;"
)
_TD_STYLE = (
    "padding:4px 10px;border:1px solid #ccc;"
    "font-family:Arial,Helvetica,sans-serif;font-size:13px;"
)

# Tells Apple Mail/iOS Mail's "Data Detectors" not to auto-link
# anything it recognises (a domain-like hostname becomes a clickable
# http://... link otherwise) - this is the standard meta tag for that.
_NO_AUTOLINK_META = (
    "<meta name=\"format-detection\" "
    "content=\"telephone=no, date=no, address=no, email=no, url=no\">"
)


# A zero-width space is a real character (unlike an HTML comment), so
# it survives into whatever flattened/rendered text a mail client's
# auto-linker scans - an HTML comment split alone was NOT enough:
# Apple Mail's Data Detectors matched the domain across the comment
# boundary anyway and wrapped each of the two surviving text runs in
# its own link to the same URL. A ZWSP is not a valid host character,
# so it reliably breaks the domain-pattern match itself.
_ZWSP = "\u200b"


def _delink_hostname(hostname):
    """Escapes a hostname for HTML, then inserts a zero-width space
    before every '.' label separator. Renders visually identical to
    the plain hostname in every mail client tested. Caveat: a direct
    copy of this HTML table cell may carry the invisible characters
    along with it - use the plain-text part of the email (in Mail.app:
    View > Message > Plain Text Alternative) if you need to paste the
    exact hostname into a command."""
    escaped = html.escape(hostname)
    return escaped.replace(".", _ZWSP + ".")


STATE_UP = "UP"
STATE_DOWN = "DOWN"
STATE_SSH_UNREACHABLE = "SSH_UNREACHABLE"

_DESCRIPTIONS = {
    STATE_UP: "reachable (ping + SSH port both OK)",
    STATE_DOWN: "UNREACHABLE (no ping response and SSH port closed/timed out)",
    STATE_SSH_UNREACHABLE: "ping OK but SSH port unreachable (sshd down, firewall change, etc.)",
}


def check_ping(host):
    """True if config.PING_COUNT ICMP echoes get at least one reply
    within config.PING_TIMEOUT_SECONDS per echo. Relies on the `ping`
    binary already installed by this role's tasks/main.yml - RHEL's
    iputils package ships a non-setuid binary with cap_net_raw already
    applied, so the unprivileged service account can run it without
    sudo or any extra capability configuration on a stock install."""
    try:
        result = subprocess.run(
            ["/usr/bin/ping", "-c", str(config.PING_COUNT),
             "-W", str(config.PING_TIMEOUT_SECONDS), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=config.PING_COUNT * config.PING_TIMEOUT_SECONDS + 5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check_ssh_port(host):
    """True if a TCP connection to config.SSH_PORT completes within
    config.SSH_TIMEOUT_SECONDS. This only confirms something is
    listening and accepting connections on the port - it deliberately
    never attempts to authenticate, so no SSH credentials or keys are
    ever needed on (or stored on) the collector."""
    try:
        with socket.create_connection(
            (host, config.SSH_PORT), timeout=config.SSH_TIMEOUT_SECONDS
        ):
            return True
    except OSError:
        return False


def classify(ping_ok, ssh_ok):
    if ping_ok and ssh_ok:
        return STATE_UP
    if not ping_ok and not ssh_ok:
        return STATE_DOWN
    return STATE_SSH_UNREACHABLE


def load_state():
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp_path = config.STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp_path, config.STATE_FILE)


def send_email(subject, text_body, html_body):
    # multipart/alternative: text_body is the plain-text fallback for
    # text-only clients, html_body (added last - the part clients
    # should prefer per RFC 2046) renders as a real table so columns
    # actually line up instead of relying on fixed-width padding in a
    # proportional font.
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_FROM
    # EMAIL_TO may be a single address (string, legacy) or a list of
    # addresses (current config.py.j2 output via |to_json) - normalize
    # so both a single recipient and a full team distribution list work.
    to_addrs = config.EMAIL_TO if isinstance(config.EMAIL_TO, list) else [config.EMAIL_TO]
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
            smtp.sendmail(config.EMAIL_FROM, to_addrs, msg.as_string())
    except (OSError, smtplib.SMTPException) as exc:
        # Relay unreachable, DNS failure, server error, etc. Log a
        # warning and report failure to the caller instead of letting
        # the exception crash the script: a failed unit logs its own
        # "<unit>: Failed with result" line, which service_failure_watcher
        # would then try to email. The caller leaves its state file
        # untouched, so the next timer run re-derives the same
        # transitions and retries the same alert.
        syslog.syslog(
            syslog.LOG_WARNING,
            "could not send alert email via %s:%s (%s) - will retry on the"
            " next run" % (config.SMTP_HOST, config.SMTP_PORT, exc),
        )
        return False
    return True


def poll_host(host, prior):
    """Checks one host and returns (record, prior_state_if_transitioned).
    prior is the host's existing state dict from the previous run (or
    {} the first time this host is ever seen). The second element is
    None unless the EFFECTIVE state (after the consecutive-failure
    threshold is applied) actually changed on this poll, in which case
    it holds the state being transitioned away from."""
    ping_ok = check_ping(host)
    ssh_ok = check_ssh_port(host)

    ping_fail_count = 0 if ping_ok else prior.get("ping_fail_count", 0) + 1
    ssh_fail_count = 0 if ssh_ok else prior.get("ssh_fail_count", 0) + 1

    prior_state = prior.get("state", STATE_UP)

    if (ping_fail_count >= config.CONSECUTIVE_FAILURES_THRESHOLD
            and ssh_fail_count >= config.CONSECUTIVE_FAILURES_THRESHOLD):
        effective_state = STATE_DOWN
    elif ssh_fail_count >= config.CONSECUTIVE_FAILURES_THRESHOLD:
        effective_state = STATE_SSH_UNREACHABLE
    else:
        effective_state = STATE_UP

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    transitioned = effective_state != prior_state
    since = now if transitioned else prior.get("since", now)

    record = {
        "ping_fail_count": ping_fail_count,
        "ssh_fail_count": ssh_fail_count,
        "state": effective_state,
        "since": since,
        "last_checked": now,
    }

    return record, (prior_state if transitioned else None)


def build_subject(transitions):
    if len(transitions) == 1:
        host, _prior_state, new_state = transitions[0]
        if new_state == STATE_UP:
            return "[%s] back UP" % host
        return "[%s] %s" % (host, new_state)
    return "%d host(s) changed availability status" % len(transitions)


def build_body(transitions, records):
    lines = [
        "%-28s %-16s -> %-16s  since %s  (%s)" % (
            host, prior_state, new_state, records[host]["since"],
            _DESCRIPTIONS[new_state],
        )
        for host, prior_state, new_state in transitions
    ]
    return (
        "host                         from state       to state          detail\n"
        "%s\n"
    ) % "\n".join(lines)


def build_body_html(transitions, records):
    rows = "".join(
        "<tr>"
        "<td style=\"%s\">%s</td>"
        "<td style=\"%s\">%s</td>"
        "<td style=\"%s\">%s</td>"
        "<td style=\"%s\">%s</td>"
        "<td style=\"%s\">%s</td>"
        "</tr>" % (
            _TD_STYLE, _delink_hostname(host),
            _TD_STYLE, html.escape(prior_state),
            _TD_STYLE, html.escape(new_state),
            _TD_STYLE, html.escape(records[host]["since"]),
            _TD_STYLE, html.escape(_DESCRIPTIONS[new_state]),
        )
        for host, prior_state, new_state in transitions
    )
    headers = "".join(
        "<th style=\"%s\">%s</th>" % (_TH_STYLE, label)
        for label in ("Host", "From state", "To state", "Since", "Detail")
    )
    return (
        "<html><head>%s</head><body>"
        "<table style=\"border-collapse:collapse;\">"
        "<tr>%s</tr>%s"
        "</table>"
        "</body></html>"
    ) % (_NO_AUTOLINK_META, headers, rows)


def main():
    state = load_state()
    records = {}
    transitions = []

    for host in config.HOSTS:
        prior = state.get(host, {})
        record, prior_state_if_transitioned = poll_host(host, prior)
        records[host] = record

        if prior_state_if_transitioned is not None:
            new_state = record["state"]
            if new_state == STATE_UP and not config.NOTIFY_RECOVERY:
                continue
            transitions.append((host, prior_state_if_transitioned, new_state))

    if transitions:
        if not send_email(
            build_subject(transitions),
            build_body(transitions, records),
            build_body_html(transitions, records),
        ):
            # Send failed (already logged to syslog) - leave the state
            # file alone so the next timer run re-derives the same
            # transitions and retries the send, and exit 0 so the unit
            # stays healthy.
            return

    save_state(records)


if __name__ == "__main__":
    main()
