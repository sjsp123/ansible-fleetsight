# idm_healthcheck_watch

Local, per-host IdM health checking, deployed to **every** `[idm]`
host (see `inventory.example.ini`). Runs `ipa-healthcheck
--output-type=json` directly on a systemd timer and logs one syslog
message per non-SUCCESS finding - it never emails anything and has no
local mail relay dependency at all. `roles/idm_healthcheck_watcher`,
on `[log_collector]` only, polls `log_db` for those messages and sends
the actual alert email - see that role's README.md for the other half
of this mechanism.

Deliberately separate from `roles/idm_healthchecks`: that role is an
ad hoc/post-patch Ansible task (run on demand, optionally failing the
play, optionally copying evidence to a share, and feeding
`roles/linuxdb_dashboard` - see that role's README.md "Dashboard
feed"). This role is a background systemd timer that runs unattended
and feeds the same check+watcher email-alerting family as
`roles/disk_space_check`/`roles/chrony_check`. Both roles can run
`ipa-healthcheck` on the same host with no conflict - they are fully
independent of one another and can be enabled/disabled separately.

## Why this runs as root

`roles/disk_space_check` and `roles/chrony_check` both run their
systemd service as an **unprivileged** system account, because
`os.statvfs()`/`timedatectl`/`chronyc` all work fine without elevated
rights. `ipa-healthcheck` is different: Red Hat's own documentation
requires it to be run as root, since several of its checks inspect
389-ds internals, certmonger state, and other root-only resources.
This role's systemd service (`idm-healthcheck-watch.service`)
therefore runs `User=root` and there is no
`idm_healthcheck_watch_user` variable to override that - this is a
deliberate, documented deviation from the rest of the check+watcher
role family, not an oversight.

## How it works

1. On a systemd timer (`idm_healthcheck_watch_poll_interval_seconds`,
   default 1800s/30min), `idm_healthcheck_watch.py` runs
   `ipa-healthcheck --output-type=json` (optionally narrowed to a
   `--source=...` subset via `idm_healthcheck_watch_mode`/
   `_extra_sources`, same three modes as `roles/idm_healthchecks`'
   `idm_hc_mode`) and parses its JSON output - the same schema
   `roles/idm_healthchecks` already parses (a list of
   `{"source", "check", "result", "kw": {"msg": ...}}` dicts).
2. `ipa-healthcheck` exits non-zero whenever it finds WARNING/ERROR/
   CRITICAL issues - that is its normal, documented behaviour, so a
   non-zero exit code alone is never treated as a run failure. Only an
   empty/unparsable/wrong-shaped result is.
3. Every entry whose `result` is not `SUCCESS` (or `PASS`, ignored
   defensively alongside it) gets one fixed-wording syslog message:
   `"idm_healthcheck_watch: {source}.{check} {result}: {msg}"`, e.g.
   `"idm_healthcheck_watch: ipahealthcheck.ds.replication.ReplicationConflictCheck.replication_conflict WARNING: 2 replication conflicts found"`
   - `WARNING` is logged at `LOG_WARNING`; `ERROR`/`CRITICAL` are both
   logged at `LOG_CRIT` (both clear `roles/rsyslog_collector`'s
   default DB severity filter of `<= 5`/notice - see that role's
   README "Database log filtering").
4. If `ipa-healthcheck` itself couldn't be run or its output couldn't
   be parsed at all (missing binary, IPA not running, a timeout after
   `idm_healthcheck_watch_timeout_seconds`, malformed JSON, ...), a
   distinct `"idm_healthcheck_watch: RUN_FAILED: {reason}"` message is
   logged at `LOG_CRIT` instead - silence from a host is exactly as
   important to notice as a real finding, so this is never silently
   dropped.
5. `roles/rsyslog_client`'s existing imjournal forwarding (already
   enabled fleet-wide, no client-side severity filtering) carries that
   message to the collector like any other journald line, where it
   lands in `log_db`'s `systemevents` table.
6. `roles/idm_healthcheck_watcher`'s poller (collector only) matches
   on this exact wording, parses out the source/check/severity/detail
   (or the run-failure reason), and emails one batched summary.

## What this does not cover

- Only what `ipa-healthcheck` itself checks on this host is covered -
  it does not (and cannot) check the health of a remote IdM replica
  from here; each `[idm]` host runs and reports its own local
  `ipa-healthcheck` independently.
- A host whose logs never reach the collector (network partition,
  rsyslog down on the client, etc.) obviously can't have its findings
  detected here either - same log-pipeline dependency as
  `roles/disk_space_watcher`/`roles/chrony_watcher`, not an
  independent heartbeat/liveness check (see
  `roles/host_availability_watcher` for that).
- Detection lands within one check interval
  (`idm_healthcheck_watch_poll_interval_seconds`, default 1800s) plus
  one poll interval on the watcher side, not instantly.
- A finding that clears (the underlying IdM issue is fixed) does not
  generate a "recovered" email - this role only logs while
  `ipa-healthcheck` still reports the issue, so there is nothing to
  alert "back to normal" on.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `idm_healthcheck_watch_enabled` | `false` | Opt-in switch (flipped to `true` fleet-wide by `group_vars/all/idm_healthcheck_watch.yml`) |
| `idm_healthcheck_watch_poll_interval_seconds` | `1800` | How often the timer runs the check |
| `idm_healthcheck_watch_install_package` | `true` | Whether to ensure the `ipa-healthcheck` package is installed |
| `idm_healthcheck_watch_mode` | `all` | `all` / `services` / `replication_topology` - same three modes as `roles/idm_healthchecks`' `idm_hc_mode` |
| `idm_healthcheck_watch_extra_sources` | `[]` | Additional `--source=...` values to append |
| `idm_healthcheck_watch_timeout_seconds` | `180` | How long a single `ipa-healthcheck` run is allowed before it's treated as a run failure |
| `idm_healthcheck_watch_app_dir` | `/opt/idm_healthcheck_watch` | Where `config.py`/`idm_healthcheck_watch.py` are deployed |

There is no `idm_healthcheck_watch_user` variable - see "Why this runs
as root" above.

## Testing it

Force a run and confirm the message appears in the journal (and, once
`roles/idm_healthcheck_watcher` is deployed, that the alert email
arrives within one watcher poll interval):

```bash
sudo systemctl start idm-healthcheck-watch.service
journalctl -t idm_healthcheck_watch --since "-2min"
```

A mocked, no-ipa-healthcheck-binary-required unit test suite covering
command building for all three modes, the non-zero-exit-is-not-a-
failure distinction, every run-failure path, SUCCESS/PASS filtering,
and the syslog priority mapping lives at
`tests/test_idm_healthcheck_watch_logic.py` (never deployed - only
`files/idm_healthcheck_watch.py` is copied to hosts):

```bash
python3 roles/idm_healthcheck_watch/tests/test_idm_healthcheck_watch_logic.py
```

## Rollout

Part of `playbooks/centralized_logging.yml`, gated by
`idm_healthcheck_watch_enabled` (on by default via
`group_vars/all/idm_healthcheck_watch.yml`), targeting `[idm]` only:

```bash
ansible-playbook playbooks/centralized_logging.yml --tags idm_healthcheck_watch
```

When `idm_healthcheck_watch_enabled` is `false`, the same playbook run
stops and disables `idm-healthcheck-watch.timer` if it already exists,
matching the teardown pattern already used for
`roles/disk_space_check`/`roles/chrony_check`.
