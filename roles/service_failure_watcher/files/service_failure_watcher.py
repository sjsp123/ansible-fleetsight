#!/usr/bin/env python3
"""
service_failure_watcher.py - polls the centralized log_db PostgreSQL
database (roles/log_db, fed by roles/rsyslog_collector) for new rows
matching systemd's own universal unit-failure message - "<unit>: Failed
with result '<reason>'." - emitted on ANY host already forwarding logs
to this collector, and emails a summary through the local mail
transport agent - see roles/service_failure_watcher/README.md.

Unlike roles/log_alerter (which alerts on ANY log line at or above a
configurable severity, and can be noisy), this only ever matches
systemd's fixed wording for a unit entering the failed state, so it
stays quiet unless something has actually failed. That message is
forwarded to this collector the same way every other journald line is
(roles/rsyslog_client's imjournal forwarding, already enabled fleet-
wide) and clears roles/rsyslog_collector's DB severity filter by
default (systemd logs it at "err" or more severe) - see this role's
README "How it works" for the full reasoning.

Run on a short systemd timer (roles/service_failure_watcher/templates/
service-failure-watcher.timer.j2), not continuously - each run is a
single poll-and-exit cycle. State (the id of the last row already
alerted on) is kept in a small JSON file at config.STATE_FILE, not in
the database itself - this reads log_db with the same SELECT-only
reader credentials log_search_ui/log_alerter already use (roles/log_db
grants that role no INSERT/UPDATE/DELETE at all on the main table -
see roles/log_db/README.md).

Pure standard library plus psycopg2 (already installed by roles/log_db
on the same host, or installed by this role directly if not) - no
Flask, no third-party frameworks, matching log_search_ui/
linuxdb_dashboard/log_alerter.
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
# attributes.
_TH_STYLE = (
    "padding:4px 10px;border:1px solid #ccc;background:#f0f0f0;"
    "text-align:left;font-family:Arial,Helvetica,sans-serif;font-size:13px;"
)
_TD_STYLE = (
    "padding:4px 10px;border:1px solid #ccc;"
    "font-family:Arial,Helvetica,sans-serif;font-size:13px;"
)
_PRE_STYLE = (
    "background:#f5f5f5;border:1px solid #ccc;padding:8px;"
    "font-family:Consolas,'Courier New',monospace;font-size:12px;"
    "white-space:pre-wrap;word-break:break-word;"
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


def _delink_hostnames_in_text(text):
    """Same zero-width-space technique as _delink_hostname, applied
    to every dotted, colon-terminated run (hostname or unit name)
    inside a larger block of already-escaped text - used for the raw
    systemd log lines, which read 'hostname: unit: Failed with
    result ...'."""
    def _sub(m):
        return m.group(0).replace(".", _ZWSP + ".")
    return re.sub(r"[A-Za-z0-9][A-Za-z0-9.-]*(?=:)", _sub, text)

# systemd's own fixed wording the instant ANY unit enters the failed
# state, regardless of unit type (service/socket/timer/mount/etc.) or
# which specific unit it is - e.g.
#   "chronyd.service: Failed with result 'signal'."
#   "sshd.service: Failed with result 'exit-code'."
# Matched as a SQL LIKE pattern passed as a bound parameter (never
# string-formatted into the query text), so no '%' escaping is needed.
FAILURE_LIKE_PATTERN = "%: Failed with result %"

# The same message, parsed in Python to pull out the unit name and
# result reason for the email subject/body.
FAILURE_RE = re.compile(r"^(?P<unit>\S+): Failed with result '(?P<result>[^']*)'")


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
        # "<unit>: Failed with result" line, which this very watcher
        # would then try to email. The caller leaves its watermark
        # unadvanced, so the next timer run retries the same alert.
        syslog.syslog(
            syslog.LOG_WARNING,
            "could not send alert email via %s:%s (%s) - will retry on the"
            " next run" % (config.SMTP_HOST, config.SMTP_PORT, exc),
        )
        return False
    return True


def parse_failure(message):
    """Best-effort split of the message into (unit, result). Falls back
    to (None, None) if a row somehow matched the LIKE pattern but
    doesn't fit the expected wording exactly - the raw message is still
    shown in the email either way, so nothing is silently dropped."""
    m = FAILURE_RE.match((message or "").strip())
    if m:
        return m.group("unit"), m.group("result")
    return None, None


def format_row(row):
    unit, result = parse_failure(row["message"])
    return "%s  %-24s  %-28s  result=%s" % (
        row["devicereportedtime"], row["fromhost"],
        unit or "(unknown unit)", result or "unknown",
    )


def fetch_max_id(cur):
    cur.execute(
        "SELECT COALESCE(MAX(id), 0) AS max_id FROM {table}".format(
            table=config.DB_TABLE
        )
    )
    return cur.fetchone()["max_id"]


def fetch_new_failures(cur, last_id):
    query = (
        "SELECT id, message, fromhost, devicereportedtime, syslogtag "
        "FROM {table} WHERE id > %s AND message LIKE %s ORDER BY id"
    ).format(table=config.DB_TABLE)
    cur.execute(query, (last_id, FAILURE_LIKE_PATTERN))
    return cur.fetchall()


def build_subject(rows, total):
    if total == 1:
        unit, _result = parse_failure(rows[0]["message"])
        return "[%s] %s FAILED" % (rows[0]["fromhost"], unit or "unit")
    hosts = sorted({r["fromhost"] for r in rows})
    return "%d service failure(s) detected across %d host(s)" % (total, len(hosts))


def build_body(rows, total):
    shown = rows[: config.MAX_ROWS_PER_EMAIL]
    lines = [format_row(r) for r in shown]
    if total > len(shown):
        lines.append(
            "... and %d more not shown (see log_search_ui for the full list)"
            % (total - len(shown))
        )
    raw = "\n".join("%s: %s" % (r["fromhost"], r["message"]) for r in shown)
    return (
        "time                        host                      unit                          result\n"
        "%s\n\n"
        "Raw log line(s):\n%s\n"
    ) % ("\n".join(lines), raw)


def build_body_html(rows, total):
    shown = rows[: config.MAX_ROWS_PER_EMAIL]
    row_cells = []
    for r in shown:
        unit, result = parse_failure(r["message"])
        row_cells.append(
            "<tr>"
            "<td style=\"%s\">%s</td>"
            "<td style=\"%s\">%s</td>"
            "<td style=\"%s\">%s</td>"
            "<td style=\"%s\">%s</td>"
            "</tr>" % (
                _TD_STYLE, html.escape(str(r["devicereportedtime"])),
                _TD_STYLE, _delink_hostname(r["fromhost"]),
                _TD_STYLE, html.escape(unit or "(unknown unit)"),
                _TD_STYLE, html.escape(result or "unknown"),
            )
        )
    headers = "".join(
        "<th style=\"%s\">%s</th>" % (_TH_STYLE, label)
        for label in ("Time", "Host", "Unit", "Result")
    )
    truncation_note = ""
    if total > len(shown):
        truncation_note = (
            "<p style=\"font-family:Arial,Helvetica,sans-serif;font-size:13px;\">"
            "... and %d more not shown (see log_search_ui for the full list)</p>"
        ) % (total - len(shown))
    raw = "\n".join("%s: %s" % (r["fromhost"], r["message"]) for r in shown)
    raw_html = _delink_hostnames_in_text(html.escape(raw))
    return (
        "<html><head>%s</head><body>"
        "<table style=\"border-collapse:collapse;\">"
        "<tr>%s</tr>%s"
        "</table>"
        "%s"
        "<p style=\"font-family:Arial,Helvetica,sans-serif;font-size:13px;\">"
        "<strong>Raw log line(s):</strong></p>"
        "<pre style=\"%s\">%s</pre>"
        "</body></html>"
    ) % (_NO_AUTOLINK_META, headers, "".join(row_cells), truncation_note, _PRE_STYLE, raw_html)


def main():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            state = load_state()
            if state is None:
                # First-ever run: bootstrap the watermark to "now" so we
                # never fire one giant email for every pre-existing
                # failure already sitting in the database - only
                # failures arriving AFTER this point are ever alerted on.
                save_state(fetch_max_id(cur))
                return

            rows = fetch_new_failures(cur, state["last_id"])

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
