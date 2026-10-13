# chrony_watcher

Centralized email alerting for the fleet-wide NTP/chrony breaches
`roles/chrony_check` logs on every `[log_clients]` host. Deployed
**only** to `[log_collector]` (i.e. `admin1.lab.example.com` in this
repo's inventory) - reads `log_db` locally, needs no per-host mail
relay, and reuses the collector's own local MTA.

## How it works

1. `roles/chrony_check` (deployed separately, to every `[log_clients]`
   host) logs a fixed-wording message to syslog whenever NTP isn't
   synchronized, chronyd isn't active, or `chronyc tracking`'s Leap
   status isn't `Normal`, e.g.:
   `"chrony_check: NTP not synchronized (timedatectl NTPSynchronized=no)"`
2. `roles/rsyslog_client`'s existing imjournal forwarding (already
   enabled fleet-wide) carries that message to this collector like any
   other journald line, landing in `log_db`'s `systemevents` table -
   the same table `roles/log_search_ui`, `roles/log_alerter`,
   `roles/service_failure_watcher`, and `roles/disk_space_watcher` all
   already read.
3. On a short systemd timer (`chrony_watcher_poll_interval_seconds`,
   default 180s), `chrony_watcher.py` connects to `log_db` read-only
   (the same `log_db_reader_user`/`log_db_reader_password` credential
   `roles/log_search_ui`/`roles/log_alerter`/`roles/disk_space_watcher`
   use) and selects every row with `id` greater than the last one it
   has already alerted on, whose `syslogtag` matches
   `LIKE 'chrony_check[%'` (bound parameter, never string-concatenated
   into the SQL). `syslogtag` is a structured field set directly by
   `chrony_check.py`'s
   `syslog.openlog(ident="chrony_check", logoption=syslog.LOG_PID)`
   call, so it always looks exactly like `chrony_check[12345]:` for
   every row this app logs - filtering on it (rather than free-text
   `message` matching) avoids the same class of false positive found
   in `roles/disk_space_watcher`'s own history (see that role's
   README "How it works" for the full incident).
4. Each matching row's message is parsed with a regex to pull out the
   issue text (e.g. `"NTP not synchronized (timedatectl
   NTPSynchronized=no)"`).
5. If there's at least one new row, one email is sent (both a plain-
   text and an HTML multipart body) summarising every breach found
   this poll, capped at `chrony_watcher_max_rows_per_email` rows
   (default 50) with a "N more not shown" note if truncated. If there
   are no new rows, nothing is sent - most poll runs, on a healthy
   fleet, send no mail at all.
6. The highest `id` seen (whether or not it triggered an email) is
   persisted to `chrony_watcher_state_file` (a small JSON file, not a
   database table) so a restart or reboot never re-alerts on
   already-seen rows and never re-sends the same breach twice. On the
   very first run ever (no state file yet), the watcher bootstraps
   quietly to the current max `id` without sending any email - this
   avoids an initial flood of alerts for pre-existing/long-standing
   breaches captured before the watcher was even deployed.

## What this does not cover

- This role only reads what `roles/chrony_check` already logged - it
  cannot detect a breach on a host that isn't running
  `roles/chrony_check`, and it cannot detect a breach whose syslog
  message never reached the collector (rsyslog down on the client,
  network partition, etc.) - same log-pipeline dependency as
  `roles/disk_space_watcher`/`roles/service_failure_watcher`.
- It is not a general-purpose log alerter - it only ever matches the
  fixed `chrony_check:` wording; unrelated log lines are ignored, by
  design (see `roles/log_alerter` for pattern-based generic alerting).
- Detection lands within one watcher poll interval
  (`chrony_watcher_poll_interval_seconds`, default 180s) of the row
  reaching `log_db`, not instantly.
- A breach that clears (NTP resyncs, chronyd restarts, leap status
  returns to Normal) does not generate a "recovered" email -
  `roles/chrony_check` only logs while a condition is abnormal, so
  there is nothing to alert "back to normal" on. Re-running the
  client-side check after the underlying issue is fixed simply stops
  producing new breach rows.

## Mail delivery

The watcher only ever talks to the **local** mail transport agent on
`chrony_watcher_smtp_host` (default `localhost`, i.e. the collector's
own Postfix/MTA) over plain SMTP on `chrony_watcher_smtp_port`
(default `25`) - it never talks to an external relay directly and
stores no relay credentials. This mirrors
`roles/disk_space_watcher`/`roles/service_failure_watcher`/
`roles/host_availability_watcher` exactly: point the collector's own
MTA config at whatever real outbound relay/smarthost your organisation
uses, and every one of these watchers benefits without any of them
needing to know about it.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `chrony_watcher_enabled` | `false` | Opt-in switch (flipped to `true` by `group_vars/all/chrony_watcher.yml`) |
| `chrony_watcher_poll_interval_seconds` | `180` | How often the timer polls `log_db` |
| `chrony_watcher_max_rows_per_email` | `50` | Cap on breach rows listed per email |
| `chrony_watcher_state_dir` / `_state_file` | `/var/lib/chrony_watcher` / `.../state.json` | Watermark persistence |
| `chrony_watcher_user` | `chrony_watch` | Unprivileged system account the timer runs as |
| `chrony_watcher_app_dir` | `/opt/chrony_watcher` | Where `config.py`/`chrony_watcher.py` are deployed |
| `chrony_watcher_db_host` / `_db_name` / `_db_table` / `_db_user` / `_db_password` | Derived from `rsyslog_collector_db_host`/`log_db_name`/`log_db_table`/`log_db_reader_user`/`log_db_reader_password` | Read-only `log_db` connection |
| `chrony_watcher_email_to` | `root` | Alert recipient |
| `chrony_watcher_email_from` | `rhel-alerts@{{ ansible_fqdn }}` | Alert sender address |
| `chrony_watcher_smtp_host` / `_smtp_port` | `localhost` / `25` | Local MTA to hand mail off to |

## Testing it

Force a breach on any `[log_clients]` host (see
`roles/chrony_check/README.md`'s "Testing it" section), then check the
collector within one poll interval:

```bash
sudo systemctl start chrony-watcher.service
journalctl -u chrony-watcher.service --since "-2min"
```

A mocked, no-database-required unit test suite covering bootstrap
behaviour, dedup/state-advance behaviour, row-cap truncation, message
parsing, the LIKE-pattern query filter, and HTML hostname de-linking
lives at `tests/test_chrony_watcher_logic.py` (never deployed - only
`files/chrony_watcher.py` is copied to hosts):

```bash
python3 roles/chrony_watcher/tests/test_chrony_watcher_logic.py
```

## Rollout

Part of `playbooks/centralized_logging.yml`, gated by
`chrony_watcher_enabled` (on by default via
`group_vars/all/chrony_watcher.yml`), targeting `[log_collector]`
only:

```bash
ansible-playbook playbooks/centralized_logging.yml --tags chrony_watcher
```

When `chrony_watcher_enabled` is `false`, the same playbook run stops
and disables `chrony-watcher.timer` if it already exists, matching the
teardown pattern already used for
`roles/disk_space_watcher`/`roles/service_failure_watcher`/
`roles/host_availability_watcher`.
