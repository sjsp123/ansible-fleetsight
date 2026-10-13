#!/usr/bin/env python3
"""
log_alerter.py - polls the centralized log_db PostgreSQL database
(roles/log_db, fed by roles/rsyslog_collector) for new rows at or more
severe than config.SEVERITY_THRESHOLD, and emails a single batched
summary through the local mail transport agent - see
roles/log_alerter/README.md.

Run on a short systemd timer (roles/log_alerter/templates/
log-alerter.timer.j2), not continuously - each run is a single
poll-and-exit cycle. State (the id of the last row already alerted on)
is kept in a small JSON file at config.STATE_FILE, not in the database
itself - this reads log_db with the same SELECT-only reader
credentials log_search_ui already uses (roles/log_db grants that role
no INSERT/UPDATE/DELETE at all on the main table - see
roles/log_db/README.md).

Pure standard library plus psycopg2 (already installed by roles/log_db
on the same host, or installed by this role directly if not) - no
Flask, no third-party frameworks, matching log_search_ui/
linuxdb_dashboard.
"""
import json
import os
import smtplib
import syslog
import socket
import sys
from email.mime.text import MIMEText
from email.utils import formatdate

import psycopg2
import psycopg2.extras

import config

SEVERITY_NAMES = {
    0: "emerg", 1: "alert", 2: "crit", 3: "err",
    4: "warning", 5: "notice", 6: "info", 7: "debug",
}


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


def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_FROM
    msg["To"] = config.EMAIL_TO
    msg["Date"] = formatdate(localtime=True)
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
            smtp.sendmail(config.EMAIL_FROM, [config.EMAIL_TO], msg.as_string())
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


def format_row(row):
    sev = SEVERITY_NAMES.get(row["priority"], str(row["priority"]))
    return "%s  %-8s  %-20s  %s  %s" % (
        row["devicereportedtime"], sev, row["fromhost"],
        row["syslogtag"] or "", row["message"],
    )


def fetch_max_id(cur):
    cur.execute(
        "SELECT COALESCE(MAX(id), 0) AS max_id FROM {table}".format(
            table=config.DB_TABLE
        )
    )
    return cur.fetchone()["max_id"]


def fetch_new_rows(cur, last_id):
    query = (
        "SELECT id, message, facility, fromhost, priority, "
        "devicereportedtime, syslogtag FROM {table} "
        "WHERE id > %s AND priority <= %s ORDER BY id"
    ).format(table=config.DB_TABLE)
    cur.execute(query, (last_id, config.SEVERITY_THRESHOLD))
    return cur.fetchall()


def build_email_body(rows, total):
    shown = rows[: config.MAX_ROWS_PER_EMAIL]
    lines = [format_row(r) for r in shown]
    if total > len(shown):
        lines.append(
            "... and %d more not shown (see log_search_ui for the full list)"
            % (total - len(shown))
        )
    sev_label = SEVERITY_NAMES.get(
        config.SEVERITY_THRESHOLD, str(config.SEVERITY_THRESHOLD)
    )
    return (
        "%d new log event(s) at severity '%s' or worse since the last check.\n\n"
        "%s\n"
    ) % (total, sev_label, "\n".join(lines))


def main():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            state = load_state()
            if state is None:
                # First-ever run: bootstrap the watermark to "now" so we
                # never fire one giant email for the entire pre-existing
                # backlog - only rows arriving AFTER this point are ever
                # alerted on.
                save_state(fetch_max_id(cur))
                return

            rows = fetch_new_rows(cur, state["last_id"])

        if not rows:
            return

        # Advance the watermark to the true max id among ALL matching
        # rows, not just the capped subset shown in the email, so
        # nothing is silently skipped or re-sent on the next run.
        new_last_id = max(r["id"] for r in rows)
        total = len(rows)

        hostname = socket.getfqdn()
        body = build_email_body(rows, total)
        if not send_email(
            "[%s] %d new log alert(s)" % (hostname, total), body
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
