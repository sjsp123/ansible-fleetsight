# disk_space_watcher

Centralized email alerting for the fleet-wide disk-space/inode
breaches `roles/disk_space_check` logs on every `[log_clients]` host.
Deployed **only** to `[log_collector]` (i.e. `admin1.lab.example.com`
in this repo's inventory) - reads `log_db` locally, needs no per-host
mail relay, and reuses the collector's own local MTA.

## How it works

1. `roles/disk_space_check` (deployed separately, to every
   `[log_clients]` host) logs a fixed-wording message to syslog
   whenever a real filesystem's disk-space or inode usage crosses its
   WARNING/CRITICAL threshold, e.g.:
   `"disk_space_check: filesystem /var space usage at 97% (threshold 95%, severity CRITICAL)"`
2. `roles/rsyslog_client`'s existing imjournal forwarding (already
   enabled fleet-wide) carries that message to this collector like any
   other journald line, landing in `log_db`'s `systemevents` table -
   the same table `roles/log_search_ui`, `roles/log_alerter`, and
   `roles/service_failure_watcher` all already read.
3. On a short systemd timer (`disk_space_watcher_poll_interval_seconds`,
   default 60s), `disk_space_watcher.py` connects to `log_db` read-only
   (the same `log_db_reader_user`/`log_db_reader_password` credential
   `roles/log_search_ui`/`roles/log_alerter` use) and selects every row
   with `id` greater than the last one it has already alerted on, whose
   `syslogtag` matches `LIKE 'disk_space_check[%'` (bound parameter,
   never string-concatenated into the SQL). `syslogtag` is a structured
   field set directly by `disk_space_check.py`'s
   `syslog.openlog(ident="disk_space_check", logoption=syslog.LOG_PID)`
   call, so it always looks exactly like `disk_space_check[12345]:` for
   every row this app logs - filtering on it (rather than free-text
   `message` matching) avoids two real problems found in testing on
   2026-09-06: (a) rsyslog's stored `message` column keeps the single
   space that follows the `tag:` separator in traditional syslog
   `tag: msg` framing, which broke an earlier start-anchored
   message-text `LIKE` pattern outright, and (b) a `sudo` audit-log
   line for one of this project's own diagnostic commands - which
   quoted this app's message wording inside its SQL text - matched a
   broadened message-text `LIKE '%...%'` filter and was nearly treated
   as a false-positive breach.
4. Each matching row's message is parsed with a regex into mount path,
   metric (`space` or `inode`), percentage, threshold, and severity.
5. If there's at least one new row, one email is sent (both a plain-
   text and an HTML multipart body) summarising every breach found
   this poll, capped at `disk_space_watcher_max_rows_per_email` rows
   (default 50) with a "N more not shown" note if truncated. If there
   are no new rows, nothing is sent - most poll runs, on a healthy
   fleet, send no mail at all.
6. The highest `id` seen (whether or not it triggered an email) is
   persisted to `disk_space_watcher_state_file` (a small JSON file, not
   a database table) so a restart or reboot never re-alerts on
   already-seen rows and never re-sends the same breach twice. On the
   very first run ever (no state file yet), the watcher bootstraps
   quietly to the current max `id` without sending any email - this
   avoids an initial flood of alerts for pre-existing/long-standing
   breaches captured before the watcher was even deployed.

## What this does not cover

- This role only reads what `roles/disk_space_check` already logged -
  it cannot detect a breach on a host that isn't running
  `roles/disk_space_check`, and it cannot detect a breach whose syslog
  message never reached the collector (rsyslog down on the client,
  network partition, etc.) - same log-pipeline dependency as
  `roles/service_failure_watcher`.
- It is not a general-purpose log alerter - it only ever matches the
  fixed `disk_space_check:` wording; unrelated log lines are ignored,
  by design (see `roles/log_alerter` for pattern-based generic
  alerting).
- Detection lands within one watcher poll interval
  (`disk_space_watcher_poll_interval_seconds`, default 60s) of the row
  reaching `log_db`, not instantly.
- A breach that clears (filesystem usage drops back below threshold)
  does not generate a "recovered" email - `roles/disk_space_check`
  only logs while a filesystem is above threshold, so there is nothing
  to alert "back to normal" on. Re-running the client-side check after
  the underlying issue is fixed simply stops producing new breach rows.

## Mail delivery

The watcher only ever talks to the **local** mail transport agent on
`disk_space_watcher_smtp_host` (default `localhost`, i.e. the
collector's own Postfix/MTA) over plain SMTP on
`disk_space_watcher_smtp_port` (default `25`) - it never talks to an
external relay directly and stores no relay credentials. This mirrors
`roles/service_failure_watcher`/`roles/host_availability_watcher`
exactly: point the collector's own MTA config at whatever real
outbound relay/smarthost your organisation uses, and every one of
these watchers benefits without any of them needing to know about it.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `disk_space_watcher_enabled` | `false` | Opt-in switch (flipped to `true` by `group_vars/all/disk_space_watcher.yml`) |
| `disk_space_watcher_poll_interval_seconds` | `60` | How often the timer polls `log_db` |
| `disk_space_watcher_max_rows_per_email` | `50` | Cap on breach rows listed per email |
| `disk_space_watcher_state_dir` / `_state_file` | `/var/lib/disk_space_watcher` / `.../state.json` | Watermark persistence |
| `disk_space_watcher_user` | `disk_space_watch` | Unprivileged system account the timer runs as |
| `disk_space_watcher_app_dir` | `/opt/disk_space_watcher` | Where `config.py`/`disk_space_watcher.py` are deployed |
| `disk_space_watcher_db_host` / `_db_name` / `_db_table` / `_db_user` / `_db_password` | Derived from `rsyslog_collector_db_host`/`log_db_name`/`log_db_table`/`log_db_reader_user`/`log_db_reader_password` | Read-only `log_db` connection |
| `disk_space_watcher_email_to` | `root` | Alert recipient |
| `disk_space_watcher_email_from` | `rhel-alerts@{{ ansible_fqdn }}` | Alert sender address |
| `disk_space_watcher_smtp_host` / `_smtp_port` | `localhost` / `25` | Local MTA to hand mail off to |

## Testing it

Force a breach on any `[log_clients]` host (see
`roles/disk_space_check/README.md`'s "Testing it" section), then check
the collector within one poll interval:

```bash
sudo systemctl start disk-space-watcher.service
journalctl -u disk-space-watcher.service --since "-2min"
```

A mocked, no-database-required unit test suite covering bootstrap
behaviour, dedup/state-advance behaviour, row-cap truncation, message
parsing, the LIKE-pattern query filter, and HTML hostname de-linking
lives at `tests/test_disk_space_watcher_logic.py` (never deployed -
only `files/disk_space_watcher.py` is copied to hosts):

```bash
python3 roles/disk_space_watcher/tests/test_disk_space_watcher_logic.py
```

## Rollout

Part of `playbooks/centralized_logging.yml`, gated by
`disk_space_watcher_enabled` (on by default via
`group_vars/all/disk_space_watcher.yml`), targeting `[log_collector]`
only:

```bash
ansible-playbook playbooks/centralized_logging.yml --tags disk_space_watcher
```

When `disk_space_watcher_enabled` is `false`, the same playbook run
stops and disables `disk-space-watcher.timer` if it already exists,
matching the teardown pattern already used for
`roles/service_failure_watcher`/`roles/host_availability_watcher`.
