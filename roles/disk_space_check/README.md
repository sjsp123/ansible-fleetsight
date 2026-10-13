# disk_space_check

Local, per-host disk-space and inode utilisation checking, deployed to
**every** `[log_clients]` host (including the collector itself, which
is also a `log_clients` member). Logs one syslog message per breach -
it never emails anything and has no local mail relay dependency at
all. `roles/disk_space_watcher`, on `[log_collector]` only, polls
`log_db` for those messages and sends the actual alert email - see
that role's README.md for the other half of this mechanism.

## How it works

1. On a short systemd timer (`disk_space_check_poll_interval_seconds`,
   default 300s/5min), `disk_space_check.py` parses `/proc/mounts` and
   walks every real (non-pseudo) mounted filesystem on the host,
   excluding the same pseudo types
   `roles/linuxdb_reports/files/df_check.sh` already excludes from its
   own WARNING/CRITICAL evaluation (`tmpfs`, `devtmpfs`, `squashfs`,
   `overlay`, `iso9660`, `efivarfs`).
2. For each real filesystem, it computes percent-used for both disk
   space and inodes via `os.statvfs()`, using the same formula GNU
   `df`/`df -i` use (`used / (used + available-to-non-root) * 100`,
   rounded up) - no subprocess call to `df`/`awk` at all, pure
   standard library.
3. Any filesystem at or above the WARNING or CRITICAL threshold (85%/
   95% by default, deliberately the same numbers already used by
   `df_check.sh` so the dashboard and these email alerts agree) gets
   one fixed-wording syslog message, e.g.:
   `"disk_space_check: filesystem /var space usage at 97% (threshold 95%, severity CRITICAL)"`
   logged at `LOG_WARNING` or `LOG_CRIT` respectively (both clear
   `roles/rsyslog_collector`'s default DB severity filter of `<= 5`/
   notice - see that role's README "Database log filtering").
4. `roles/rsyslog_client`'s existing imjournal forwarding (already
   enabled fleet-wide, no client-side severity filtering) carries that
   message to the collector like any other journald line, where it
   lands in `log_db`'s `systemevents` table.
5. `roles/disk_space_watcher`'s poller (collector only) matches on
   this exact wording, parses out the mount/metric/percentage/
   threshold/severity, and emails one batched summary.

## What this does not cover

- Only filesystems visible via `/proc/mounts` on the host actually
  running this check are covered - it does not (and cannot) check
  free space on a remote NFS server itself, only the client-side mount
  point's own reported usage.
- Inode usage is reported as "no data" (skipped, never a false alert)
  for any filesystem that reports zero total inodes - some network/
  pseudo filesystems do this.
- A host whose logs never reach the collector (network partition,
  rsyslog down on the client, etc.) obviously can't have its breaches
  detected here either - same log-pipeline dependency as
  `roles/service_failure_watcher`, not an independent heartbeat/
  liveness check (see `roles/host_availability_watcher` for that).
- Detection lands within one check interval
  (`disk_space_check_poll_interval_seconds`, default 300s) plus one
  poll interval on the watcher side, not instantly.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `disk_space_check_enabled` | `false` | Opt-in switch (flipped to `true` fleet-wide by `group_vars/all/disk_space_check.yml`) |
| `disk_space_check_poll_interval_seconds` | `300` | How often the timer runs the check |
| `disk_space_check_space_warn_threshold` / `_space_crit_threshold` | `85` / `95` | Disk-space percent-used thresholds (matches `df_check.sh`) |
| `disk_space_check_inode_warn_threshold` / `_inode_crit_threshold` | `85` / `95` | Inode percent-used thresholds |
| `disk_space_check_excluded_fstypes` | `tmpfs, devtmpfs, squashfs, overlay, iso9660, efivarfs` | Filesystem types never checked |
| `disk_space_check_user` | `disk_space_chk` | Unprivileged system account the timer runs as |
| `disk_space_check_app_dir` | `/opt/disk_space_check` | Where `config.py`/`disk_space_check.py` are deployed |

## Testing it

Force a breach on a host already in `[log_clients]` and confirm the
message appears in the journal (and, once
`roles/disk_space_watcher` is deployed, that the alert email arrives
within one watcher poll interval):

```bash
sudo systemctl start disk-space-check.service
journalctl -t disk_space_check --since "-2min"
```

To exercise the threshold/wording logic directly without waiting on an
actually-full filesystem, temporarily lower a threshold to something
any host will already clear:

```bash
sudo ansible-playbook playbooks/centralized_logging.yml \
  --limit client1.lab.example.com --tags disk_space_check \
  -e disk_space_check_space_warn_threshold=1
sudo systemctl start disk-space-check.service
journalctl -t disk_space_check --since "-2min"
# Remember to re-run without the -e override afterwards to restore 85.
```

A mocked, no-filesystem-required unit test suite covering `/proc/
mounts` parsing (including pseudo-fs exclusion and octal-escape
handling), the percent-used formulas, threshold classification, and
syslog emission lives at
`tests/test_disk_space_check_logic.py` (never deployed - only
`files/disk_space_check.py` is copied to hosts):

```bash
python3 roles/disk_space_check/tests/test_disk_space_check_logic.py
```

## Rollout

Part of `playbooks/centralized_logging.yml`, gated by
`disk_space_check_enabled` (on by default via
`group_vars/all/disk_space_check.yml`), targeting `[log_clients]`
(every host, including the collector):

```bash
ansible-playbook playbooks/centralized_logging.yml --tags disk_space_check
```

When `disk_space_check_enabled` is `false`, the same playbook run
stops and disables `disk-space-check.timer` if it already exists,
matching the teardown pattern already used for
`roles/service_failure_watcher`/`roles/host_availability_watcher`.
