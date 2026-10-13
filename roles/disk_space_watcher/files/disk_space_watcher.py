#!/usr/bin/env python3
"""
disk_space_watcher.py - polls the centralized log_db PostgreSQL
database (roles/log_db, fed by roles/rsyslog_collector) for new rows
matching roles/disk_space_check's fixed alert wording - emitted on ANY
host already forwarding logs to this collector, the instant a local
filesystem's disk-space or inode usage crosses its configured
threshold - and emails a summary through the local mail transport
agent - see roles/disk_space_watcher/README.md.

Same architecture as roles/service_failure_watcher: pure standard
library plus psycopg2, a single poll-and-exit cycle on a short systemd
timer, watermark state kept in a small JSON file (not the database),
SELECT-only log_db_reader_user/_password credentials - no INSERT/
UPDATE/DELETE privilege needed on log_db at all.

This role is only ever deployed to [log_collector] (see
playbooks/centralized_logging.yml) - it reads log_db locally and
relies on roles/disk_space_check (deployed to every [log_clients]
host, including the collector itself) actually running the local
checks and logging any breach.
"""
import html
import json
import os
import re
import smtplib
import syslog
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

import psycopg2
import psycopg2.extras

import config

# Shared inline styles for the HTML table part of the alert email -
# inline (not a <style> block) because most mail clients strip
# <head>/<style> content and only reliably honour inline style=
# attributes. Same values as roles/service_failure_watcher for a
# consistent look across every alert email this fleet sends.
_TH_STYLE = (
    "padding:4px 10px;border:1px solid #ccc;background:#f0f0f0;"
    "text-align:left;font-family:Arial,Helvetica,sans-serif;font-size:13px;"
)
_TD_STYLE = (
    "padding:4px 10px;border:1px solid #ccc;"
    "font-family:Arial,Helvetica,sans-serif;font-size:13px;"
)
_SEVERITY_STYLE = {
    "CRITICAL": _TD_STYLE + "color:#a13544;font-weight:bold;",
    "WARNING": _TD_STYLE + "color:#964219;font-weight:bold;",
}

# Tells Apple Mail/iOS Mail's "Data Detectors" not to auto-link
# anything it recognises (a domain-like hostname becomes a clickable
# http://... link otherwise) - this is the standard meta tag for that.
_NO_AUTOLINK_META = (
    "<meta name=\"format-detection\" "
    "content=\"telephone=no, date=no, address=no, email=no, url=no\">"
)

# A zero-width space is a real character (unlike an HTML comment), so
# it survives into whatever flattened/rendered text a mail client's
# auto-linker scans - see roles/service_failure_watcher/files/
# service_failure_watcher.py for the full reasoning (an HTML-comment
# split alone was tried there first and found insufficient).
_ZWSP = "\u200b"


def _delink_hostname(hostname):
    """Escapes a hostname for HTML, then inserts a zero-width space
    before every '.' label separator. Caveat: a direct copy of this
    HTML table cell may carry the invisible characters along with it -
    use the plain-text part of the email if you need to paste the
    exact hostname into a command."""
    escaped = html.escape(hostname)
    return escaped.replace(".", _ZWSP + ".")


# The syslogtag column is a structured field set directly by
# roles/disk_space_check's syslog.openlog(ident="disk_space_check",
# logoption=syslog.LOG_PID, ...) call - every row this app ever logs
# has syslogtag looking exactly like "disk_space_check[12345]:", with
# no leading whitespace and no realistic chance of an unrelated log
# line coincidentally matching it (unlike the free-text "message"
# column: on 2026-09-06 one of this very script's own diagnostic
# `sudo python3 -c "..."` commands - which quoted this app's message
# wording inside its own SQL text - got logged by sudo's syslog audit
# trail and matched a message-text LIKE filter, almost surfacing as a
# false-positive breach). Filtering on syslogtag instead of message
# text avoids that whole class of false positive, and also sidesteps
# an earlier bug where the message column's stored value turned out to
# have a leading space (from rsyslog's "tag: msg" framing) that broke
# a start-anchored message-text LIKE pattern. Matched as a SQL LIKE
# pattern passed as a bound parameter (never string-formatted into the
# query text), so no '%' escaping is needed.
ALERT_SYSLOGTAG_PATTERN = "disk_space_check[%"

ALERT_RE = re.compile(
    r"^disk_space_check: filesystem (?P<mount>\S+) (?P<metric>space|inode) usage at "
    r"(?P<pct>\d+)% \(threshold (?P<threshold>\d+)%, severity (?P<severity>WARNING|CRITICAL)\)$"
)


def get_connection():
    return psycopg2.connect(
        host=config.DB_HOST,
        dbname=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        connect_timeout=5,
    )


def load_state():
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(last_id):
    tmp_path = config.STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump({"last_id": last_id}, fh)
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
        # would then try to email. The caller leaves its watermark
        # unadvanced, so the next timer run retries the same alert.
        syslog.syslog(
            syslog.LOG_WARNING,
            "could not send alert email via %s:%s (%s) - will retry on the"
            " next run" % (config.SMTP_HOST, config.SMTP_PORT, exc),
        )
        return False
    return True


def parse_alert(message):
    """Best-effort split of the message into (mount, metric, pct,
    threshold, severity). Falls back to all-None if a row somehow
    matched the LIKE pattern but doesn't fit the expected wording
    exactly - the raw message is still shown in the email either way,
    nothing is silently dropped."""
    m = ALERT_RE.match((message or "").strip())
    if m:
        return (
            m.group("mount"),
            m.group("metric"),
            m.group("pct"),
            m.group("threshold"),
            m.group("severity"),
        )
    return None, None, None, None, None


def format_row(row):
    mount, metric, pct, threshold, severity = parse_alert(row["message"])
    return "%s  %-24s  %-20s  %-6s  %3s%% (threshold %s%%)  %s" % (
        row["devicereportedtime"],
        row["fromhost"],
        mount or "(unknown mount)",
        metric or "?",
        pct or "?",
        threshold or "?",
        severity or "UNKNOWN",
    )


def fetch_max_id(cur):
    cur.execute(
        "SELECT COALESCE(MAX(id), 0) AS max_id FROM {table}".format(
            table=config.DB_TABLE
        )
    )
    return cur.fetchone()["max_id"]


def fetch_new_alerts(cur, last_id):
    query = (
        "SELECT id, message, fromhost, devicereportedtime, syslogtag "
        "FROM {table} WHERE id > %s AND syslogtag LIKE %s ORDER BY id"
    ).format(table=config.DB_TABLE)
    cur.execute(query, (last_id, ALERT_SYSLOGTAG_PATTERN))
    return cur.fetchall()


def build_subject(rows, total):
    if total == 1:
        mount, metric, pct, _threshold, severity = parse_alert(rows[0]["message"])
        return "[%s] %s %s usage %s%% (%s)" % (
            rows[0]["fromhost"],
            mount or "filesystem",
            metric or "usage",
            pct or "?",
            severity or "ALERT",
        )
    hosts = sorted({r["fromhost"] for r in rows})
    severities = {parse_alert(r["message"])[4] for r in rows}
    worst = "CRITICAL" if "CRITICAL" in severities else "WARNING"
    return "%d disk/inode threshold breach(es) detected across %d host(s) (%s)" % (
        total,
        len(hosts),
        worst,
    )


def build_body(rows, total):
    shown = rows[: config.MAX_ROWS_PER_EMAIL]
    lines = [format_row(r) for r in shown]
    if total > len(shown):
        lines.append(
            "... and %d more not shown (see log_search_ui for the full list)"
            % (total - len(shown))
        )
    return (
        "time                        host                      mount                 metric  usage      severity\n"
        "%s\n"
    ) % "\n".join(lines)


def build_body_html(rows, total):
    shown = rows[: config.MAX_ROWS_PER_EMAIL]
    row_cells = []
    for r in shown:
        mount, metric, pct, threshold, severity = parse_alert(r["message"])
        severity_style = _SEVERITY_STYLE.get(severity, _TD_STYLE)
        row_cells.append(
            "<tr>"
            "<td style=\"%s\">%s</td>"
            "<td style=\"%s\">%s</td>"
            "<td style=\"%s\">%s</td>"
            "<td style=\"%s\">%s</td>"
            "<td style=\"%s\">%s</td>"
            "<td style=\"%s\">%s</td>"
            "</tr>"
            % (
                _TD_STYLE, html.escape(str(r["devicereportedtime"])),
                _TD_STYLE, _delink_hostname(r["fromhost"]),
                _TD_STYLE, html.escape(mount or "(unknown mount)"),
                _TD_STYLE, html.escape(metric or "?"),
                _TD_STYLE, html.escape("%s%% (threshold %s%%)" % (pct or "?", threshold or "?")),
                severity_style, html.escape(severity or "UNKNOWN"),
            )
        )
    headers = "".join(
        "<th style=\"%s\">%s</th>" % (_TH_STYLE, label)
        for label in ("Time", "Host", "Mount", "Metric", "Usage", "Severity")
    )
    truncation_note = ""
    if total > len(shown):
        truncation_note = (
            "<p style=\"font-family:Arial,Helvetica,sans-serif;font-size:13px;\">"
            "... and %d more not shown (see log_search_ui for the full list)</p>"
        ) % (total - len(shown))
    return (
        "<html><head>%s</head><body>"
        "<table style=\"border-collapse:collapse;\">"
        "<tr>%s</tr>%s"
        "</table>"
        "%s"
        "</body></html>"
    ) % (_NO_AUTOLINK_META, headers, "".join(row_cells), truncation_note)


def main():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            state = load_state()
            if state is None:
                # First-ever run: bootstrap the watermark to "now" so
                # we never fire one giant email for every pre-existing
                # breach already sitting in the database - only
                # breaches arriving AFTER this point are ever alerted
                # on.
                save_state(fetch_max_id(cur))
                return

            rows = fetch_new_alerts(cur, state["last_id"])

        if not rows:
            return

        # Advance the watermark to the true max id among ALL matching
        # rows, not just the capped subset shown in the email, so
        # nothing is silently skipped or re-sent on the next run.
        new_last_id = max(r["id"] for r in rows)
        total = len(rows)

        if not send_email(
            build_subject(rows, total),
            build_body(rows, total),
            build_body_html(rows, total),
        ):
            # Send failed (already logged to syslog) - leave the
            # watermark where it is so the next timer run retries
            # these same rows, and exit 0 so the unit stays healthy.
            return
        save_state(new_last_id)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
