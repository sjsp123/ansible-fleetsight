# idm_healthcheck_watcher

Centralized email alerting for the fleet-wide IdM healthcheck findings
`roles/idm_healthcheck_watch` logs on every `[idm]` host. Deployed
**only** to `[log_collector]` (i.e. `admin1.lab.example.com` in this
repo's inventory) - reads `log_db` locally, needs no per-host mail
relay, and reuses the collector's own local MTA.

## How it works

1. `roles/idm_healthcheck_watch` (deployed separately, to every
   `[idm]` host) logs a fixed-wording message to syslog whenever a
   local `ipa-healthcheck` run finds a non-SUCCESS issue, e.g.:
   `"idm_healthcheck_watch: ipahealthcheck.ds.replication.ReplicationConflictCheck.replication_conflict WARNING: 2 replication conflicts found"`
   - or, if `ipa-healthcheck` itself couldn't be run at all:
   `"idm_healthcheck_watch: RUN_FAILED: ipa-healthcheck timed out after 180s"`
2. `roles/rsyslog_client`'s existing imjournal forwarding (already
   enabled fleet-wide) carries that message to this collector like any
   other journald line, landing in `log_db`'s `systemevents` table -
   the same table `roles/log_search_ui`, `roles/log_alerter`, and
   `roles/disk_space_watcher`/`roles/chrony_watcher` all already read.
3. On a short systemd timer (`idm_healthcheck_watcher_poll_interval_seconds`,
   default 60s), `idm_healthcheck_watcher.py` connects to `log_db`
   read-only (the same `log_db_reader_user`/`log_db_reader_password`
   credential `roles/log_search_ui`/`roles/disk_space_watcher` use) and
   selects every row with `id` greater than the last one it has
   already alerted on, whose `syslogtag` matches
   `LIKE 'idm_healthcheck_watch[%'` (bound parameter, never string-
   concatenated into the SQL). `syslogtag` is a structured field set
   directly by `idm_healthcheck_watch.py`'s
   `syslog.openlog(ident="idm_healthcheck_watch", logoption=syslog.LOG_PID)`
   call, so it always looks exactly like
   `idm_healthcheck_watch[12345]:` for every row this app logs -
   filtering on it (rather than free-text `message` matching) avoids
   the same class of false positive `roles/disk_space_watcher`
   documented finding on 2026-09-06 (a `sudo` audit-log line that
   happened to quote another app's alert wording inside its own
   command text, and would have matched a broadened message-text
   `LIKE` filter despite being unrelated).
4. Each matching row's message is parsed with a regex into either
   `(source, check, result, msg)` for a per-finding issue, or a
   `RUN_FAILED` shape with the failure reason - `result` is normalised
   to the literal string `RUN_FAILED` for the latter so it always
   sorts/styles as the most severe outcome.
5. If there's at least one new row, one email is sent (both a plain-
   text and an HTML multipart body) summarising every finding this
   poll, capped at `idm_healthcheck_watcher_max_rows_per_email` rows
   (default 50) with a "N more not shown" note if truncated. If there
   are no new rows, nothing is sent - most poll runs, on a healthy
   fleet, send no mail at all.
6. The highest `id` seen (whether or not it triggered an email) is
   persisted to `idm_healthcheck_watcher_state_file` (a small JSON
   file, not a database table) so a restart or reboot never re-alerts
   on already-seen rows and never re-sends the same finding twice. On
   the very first run ever (no state file yet), the watcher bootstraps
   quietly to the current max `id` without sending any email - this
   avoids an initial flood of alerts for pre-existing/long-standing
   findings captured before the watcher was even deployed.

## What this does not cover

- This role only reads what `roles/idm_healthcheck_watch` already
  logged - it cannot detect a finding on a host that isn't running
  `roles/idm_healthcheck_watch`, and it cannot detect a finding whose
  syslog message never reached the collector (rsyslog down on the
  client, network partition, etc.) - same log-pipeline dependency as
  `roles/disk_space_watcher`.
- It is not a general-purpose log alerter - it only ever matches the
  fixed `idm_healthcheck_watch:` wording; unrelated log lines are
  ignored, by design (see `roles/log_alerter` for pattern-based
  generic alerting).
- Detection lands within one watcher poll interval
  (`idm_healthcheck_watcher_poll_interval_seconds`, default 60s) of
  the row reaching `log_db`, not instantly.
- A finding that clears does not generate a "recovered" email - see
  `roles/idm_healthcheck_watch/README.md` "What this does not cover".

## Mail delivery

The watcher only ever talks to the **local** mail transport agent on
`idm_healthcheck_watcher_smtp_host` (default `localhost`, i.e. the
collector's own Postfix/MTA) over plain SMTP on
`idm_healthcheck_watcher_smtp_port` (default `25`) - it never talks to
an external relay directly and stores no relay credentials. This
mirrors `roles/disk_space_watcher`/`roles/chrony_watcher` exactly:
point the collector's own MTA config at whatever real outbound relay/
smarthost your organisation uses, and every one of these watchers
benefits without any of them needing to know about it.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `idm_healthcheck_watcher_enabled` | `false` | Opt-in switch (flipped to `true` by `group_vars/all/idm_healthcheck_watcher.yml`) |
| `idm_healthcheck_watcher_poll_interval_seconds` | `60` | How often the timer polls `log_db` |
| `idm_healthcheck_watcher_max_rows_per_email` | `50` | Cap on finding rows listed per email |
| `idm_healthcheck_watcher_state_dir` / `_state_file` | `/var/lib/idm_healthcheck_watcher` / `.../state.json` | Watermark persistence |
| `idm_healthcheck_watcher_user` | `idm_hc_watch` | Unprivileged system account the timer runs as |
| `idm_healthcheck_watcher_app_dir` | `/opt/idm_healthcheck_watcher` | Where `config.py`/`idm_healthcheck_watcher.py` are deployed |
| `idm_healthcheck_watcher_db_host` / `_db_name` / `_db_table` / `_db_user` / `_db_password` | Derived from `rsyslog_collector_db_host`/`log_db_name`/`log_db_table`/`log_db_reader_user`/`log_db_reader_password` | Read-only `log_db` connection |
| `idm_healthcheck_watcher_email_to` | `root` | Alert recipient |
| `idm_healthcheck_watcher_email_from` | `rhel-alerts@{{ ansible_fqdn }}` | Alert sender address |
| `idm_healthcheck_watcher_smtp_host` / `_smtp_port` | `localhost` / `25` | Local MTA to hand mail off to |

## Testing it

Force a finding on any `[idm]` host (see
`roles/idm_healthcheck_watch/README.md`'s "Testing it" section), then
check the collector within one poll interval:

```bash
sudo systemctl start idm-healthcheck-watcher.service
journalctl -u idm-healthcheck-watcher.service --since "-2min"
```

A mocked, no-database-required unit test suite covering bootstrap
behaviour, dedup/state-advance behaviour, row-cap truncation, both
message shapes (per-finding issue and `RUN_FAILED`), the LIKE-pattern
query filter, worst-severity subject selection, and HTML hostname
de-linking lives at `tests/test_idm_healthcheck_watcher_logic.py`
(never deployed - only `files/idm_healthcheck_watcher.py` is copied to
hosts):

```bash
python3 roles/idm_healthcheck_watcher/tests/test_idm_healthcheck_watcher_logic.py
```

## Rollout

Part of `playbooks/centralized_logging.yml`, gated by
`idm_healthcheck_watcher_enabled` (on by default via
`group_vars/all/idm_healthcheck_watcher.yml`), targeting
`[log_collector]` only:

```bash
ansible-playbook playbooks/centralized_logging.yml --tags idm_healthcheck_watcher
```

When `idm_healthcheck_watcher_enabled` is `false`, the same playbook
run stops and disables `idm-healthcheck-watcher.timer` if it already
exists, matching the teardown pattern already used for
`roles/disk_space_watcher`/`roles/chrony_watcher`.
