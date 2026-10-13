# log_alerter

Near-real-time email alerting from the centralized log database
(`roles/log_db`, fed continuously by `roles/rsyslog_collector`),
instead of only noticing something while browsing `log_search_ui` or
waiting for the next `hostinfo`/`linuxdb_reports` cron run. A short
systemd timer polls for new rows at or above a configurable severity
and emails one batched summary.

## How it works

1. `log-alerter.timer` runs `log-alerter.service` every
   `log_alerter_poll_interval_seconds` (default 60s).
2. Each run, `log_alerter.py`:
   - Connects to `log_db` read-only, using the same SELECT-only
     reader credentials `log_search_ui` already uses
     (`log_db_reader_user`/`log_db_reader_password`).
   - Reads the id of the last row it already alerted on from
     `log_alerter_state_file` (a small JSON file - not the database).
   - Queries for rows with `id > last_id AND priority <= threshold`.
   - If there are any, sends **one** email listing them (capped at
     `log_alerter_max_rows_per_email`, with a "+N more, see
     log_search_ui" note if truncated), then advances the state file
     to the true max id among **all** matching rows - not just the
     capped subset shown - so nothing is silently skipped or re-sent.
   - If there are none, it exits quietly - no email, no log noise.
3. **First-ever run**: there is no state file yet, so the script
   bootstraps its watermark to the database's current `MAX(id)` and
   exits without sending anything. This avoids one enormous email
   containing the entire pre-existing backlog the first time this
   role is deployed.

This is "near-real-time" rather than instant - worst case, a new
matching event waits up to one poll interval (60s by default) before
being included in an alert email. For truly instant alerting on
specific systemd service failures, see the companion
`roles/service_failure_alert` role.

## Choosing the threshold

`log_alerter_severity_threshold` uses RFC 5424 syslog severity
numbering: **lower number = more severe** (0=emerg, 1=alert, 2=crit,
3=err, 4=warning, 5=notice, 6=info, 7=debug). The default, `4`
(warning and worse), deliberately does **not** use the
authpriv/cron "always keep" carve-out that `roles/rsyslog_collector`'s
DB write filter uses - that carve-out exists to make sure routine
notice/info-level sudo and cron activity is never lost for audit
purposes, not because that routine activity deserves an email every
time it happens.

**This can only alert on rows that actually made it into the
database.** `roles/rsyslog_collector`'s DB filter
(`rsyslog_collector_db_min_severity`, default `5`=notice) already
drops anything less severe than notice before it ever reaches
`log_db` (except the authpriv/cron carve-out, which is stored at
every severity regardless). Keep `log_alerter_severity_threshold`
**numerically >=** `rsyslog_collector_db_min_severity`
(i.e. no *more* severe/selective than the DB filter) - the default `4`
sits safely inside the default `5` window. If you ever tighten
`rsyslog_collector_db_min_severity` to a lower number, lower this
value to match, or you will silently stop being alerted on things you
can still see with a warning triangle in `log_search_ui` (because they
simply never reached the database rows this role reads).

## Mail delivery

Same as `roles/service_failure_alert`: the script always sends
through the **local** mail transport agent (`localhost:25` by
default) - it never talks to an external SMTP relay and never handles
relay credentials. See `roles/service_failure_alert/README.md` "Mail
delivery" for the home-lab-vs-work-PoC Postfix setup notes (identical
requirement, not repeated per-role).

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `log_alerter_enabled` | `false` | Opt-in toggle (this role's own default). See `group_vars/all/log_db.yml` for the fleet-wide override. |
| `log_alerter_poll_interval_seconds` | `60` | Timer cadence. |
| `log_alerter_severity_threshold` | `4` (warning) | RFC 5424 severity; lower number = more severe. Must stay >= `rsyslog_collector_db_min_severity`. |
| `log_alerter_max_rows_per_email` | `50` | Cap on rows listed per email. |
| `log_alerter_state_dir` / `_state_file` | `/var/lib/log_alerter` / `.../state.json` | Dedup watermark storage. |
| `log_alerter_user` | `log_alerter` | Unprivileged account the timer/service runs as. |
| `log_alerter_app_dir` | `/opt/log_alerter` | Script + config install location. |
| `log_alerter_db_host` / `_db_name` / `_db_table` / `_db_user` / `_db_password` | Mirror the matching `log_db_*`/`rsyslog_collector_db_host` variables | Read-only DB connection - same pattern as `roles/log_search_ui`. |
| `log_alerter_email_to` | `root` | Alert email recipient. |
| `log_alerter_email_from` | `rhel-alerts@<fqdn>` | Alert email sender. |
| `log_alerter_smtp_host` / `_smtp_port` | `localhost` / `25` | Local MTA the script talks to. |

## Testing it

1. Confirm the timer is active:
   ```
   systemctl status log-alerter.timer
   systemctl list-timers log-alerter.timer
   ```
2. Generate a test warning-level event that will actually reach the
   database (severity 4 is well inside the default DB filter's notice
   threshold):
   ```
   logger -p user.warning "log_alerter test: this is a synthetic warning event"
   ```
3. Wait up to one poll interval (60s by default), then check your
   inbox for a `[<hostname>] 1 new log alert(s)` email containing that
   line.
4. Confirm dedup works - run the service again manually and confirm no
   second email arrives for the same row:
   ```
   sudo systemctl start log-alerter.service
   journalctl -u log-alerter.service -n 20 --no-pager
   ```
5. Inspect the state file to confirm the watermark advanced:
   ```
   sudo cat /var/lib/log_alerter/state.json
   ```

If you want to verify against the database directly first (useful
when troubleshooting rather than the "did an email arrive" black-box
test above):
```
sudo -u postgres psql -d syslog -c \
  "SELECT id, devicereportedtime, priority, fromhost, message FROM systemevents WHERE priority <= 4 ORDER BY id DESC LIMIT 20;"
```

## Rollout

```
ansible-playbook playbooks/centralized_logging.yml --tags log_alerter
```

or as part of a full `centralized_logging.yml` run (on by default via
`group_vars/all/log_db.yml`, same as `log_db`/`log_search_ui`).
