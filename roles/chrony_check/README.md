# chrony_check

Local, per-host NTP/chrony health checking, deployed to **every**
`[log_clients]` host (including the collector itself, which is also a
`log_clients` member). Logs one syslog message per breach - it never
emails anything and has no local mail relay dependency at all.
`roles/chrony_watcher`, on `[log_collector]` only, polls `log_db` for
those messages and sends the actual alert email - see that role's
README.md for the other half of this mechanism.

## How it works

1. On a short systemd timer (`chrony_check_poll_interval_seconds`,
   default 900s/15min - NTP/chrony drift is normally a slow-moving
   condition), `chrony_check.py` runs three independent, read-only
   checks against fixed argv lists (no shell string, so there is no
   command-injection surface at all):
   - `timedatectl show -p NTPSynchronized --value` - flags anything
     other than `yes`.
   - `systemctl is-active chronyd` - flags anything other than
     `active`.
   - `chronyc tracking`'s `Leap status` line - flags anything other
     than `Normal`.
   These mirror exactly what `roles/linuxdb_dashboard` already flags
   as a WARNING on the fleet dashboard (see
   `group_vars/all/host_checks.yml`'s `time_sync`/`chronyd_status`/
   `chrony_leap` checks and `roles/linuxdb_dashboard/files/app.py`'s
   `NTP_SYNC`/`CHRONYD` flagging logic), so what triggers an email here
   lines up with what the dashboard already shows.
2. Any breach gets one fixed-wording syslog message at `LOG_WARNING`,
   e.g.:
   `"chrony_check: NTP not synchronized (timedatectl NTPSynchronized=no)"`
   (clears `roles/rsyslog_collector`'s default DB severity filter of
   `<= 5`/notice - see that role's README "Database log filtering").
   A command that can't be run at all (missing binary, timeout) is
   treated as "unknown" and never alerted on - only a confirmed
   abnormal state raises an issue.
3. `roles/rsyslog_client`'s existing imjournal forwarding (already
   enabled fleet-wide, no client-side severity filtering) carries that
   message to the collector like any other journald line, where it
   lands in `log_db`'s `systemevents` table.
4. `roles/chrony_watcher`'s poller (collector only) matches on this
   exact wording and emails one batched summary.

## What this does not cover

- All three checks are read-only and local to the host running them -
  a host whose logs never reach the collector (network partition,
  rsyslog down on the client, etc.) obviously can't have its breaches
  detected here either - same log-pipeline dependency as
  `roles/disk_space_check`/`roles/service_failure_watcher`, not an
  independent heartbeat/liveness check (see
  `roles/host_availability_watcher` for that).
- There is no severity distinction between the three conditions -
  every breach is logged at `WARNING`, matching the dashboard's own
  WARNING-only treatment of `NTP_SYNC`/`CHRONYD`.
- Detection lands within one check interval
  (`chrony_check_poll_interval_seconds`, default 900s) plus one poll
  interval on the watcher side, not instantly.
- A breach that clears (NTP resyncs, chronyd restarts, leap status
  returns to Normal) does not generate a "recovered" email - this
  check only logs while a condition is abnormal, so there is nothing
  to alert "back to normal" on.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `chrony_check_enabled` | `false` | Opt-in switch (flipped to `true` fleet-wide by `group_vars/all/chrony_check.yml`) |
| `chrony_check_poll_interval_seconds` | `900` | How often the timer runs the check |
| `chrony_check_user` | `chrony_chk` | Unprivileged system account the timer runs as |
| `chrony_check_app_dir` | `/opt/chrony_check` | Where `chrony_check.py` is deployed |

## Testing it

Force a breach on a host already in `[log_clients]` and confirm the
message appears in the journal (and, once `roles/chrony_watcher` is
deployed, that the alert email arrives within one watcher poll
interval):

```bash
sudo systemctl stop chronyd
sudo systemctl start chrony-check.service
journalctl -t chrony_check --since "-2min"
# Remember to start chronyd again afterwards.
sudo systemctl start chronyd
```

A mocked, no-real-chrony-required unit test suite covering each of the
three checks (via a stubbed `subprocess.run`), leap-status parsing,
the end-to-end `check_all()` issue list, and syslog emission (including
the no-alert / nothing-logged case) lives at
`tests/test_chrony_check_logic.py` (never deployed - only
`files/chrony_check.py` is copied to hosts):

```bash
python3 roles/chrony_check/tests/test_chrony_check_logic.py
```

## Rollout

Part of `playbooks/centralized_logging.yml`, gated by
`chrony_check_enabled` (on by default via
`group_vars/all/chrony_check.yml`), targeting `[log_clients]` (every
host, including the collector):

```bash
ansible-playbook playbooks/centralized_logging.yml --tags chrony_check
```

When `chrony_check_enabled` is `false`, the same playbook run stops
and disables `chrony-check.timer` if it already exists, matching the
teardown pattern already used for
`roles/disk_space_check`/`roles/service_failure_watcher`/
`roles/host_availability_watcher`.
