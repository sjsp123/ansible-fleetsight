# Storage Sizing & LVM Setup for the Log Collector Host

This covers the one host that matters for storage planning: whichever
host you put in the `[log_collector]` inventory group (`admin1` in the
lab). That single host is where three independent things accumulate
data, per `playbooks/centralized_logging.yml` and
`playbooks/linuxdb_dashboard.yml`:

| Path                 | What lives there                                                          | Retention control                                    | Role                                        |
| -------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------- | -------------------------------------------- |
| `/var/log/remote`    | Raw per-host/per-program flat log files, `gzip`-compressed after 1 day     | `rsyslog_remote_log_retain_days` (default **14** days) | `roles/rsyslog_collector`                    |
| `/var/lib/pgsql`     | PostgreSQL data directory — every message ALSO lands here, uncompressed, plus a full-text (`gin_trgm_ops`) search index | `log_db_retention_days` (default **90** days)          | `roles/log_db` + `roles/rsyslog_collector`   |
| `/linuxdb`           | Timestamped diagnostic/report/config-snapshot history + `hostinfo.csv`    | `*_keep_runs` (30/60/30, see each role's README)      | `roles/linuxdb_reports`, `config_snapshot`, `hostinfo` |

**The PostgreSQL data directory is what will dominate total disk use, by
a wide margin** — it independently stores everything rsyslog receives
(same messages as `/var/log/remote`, not a subset), uncompressed, for
6.4x longer by default (90 days vs. 14), plus a trigram GIN index on
the message text that's frequently as large as the table itself. Every
number below is driven by that, not by `/var/log/remote`.

## Do this first: measure your own lab, don't guess

The read-only measurement commands in this section are also bundled as
one script — `bash scripts/collect_storage_sizing.sh` runs them all
against the collector and prints the same numbers in one pass (see
`scripts/tests/` for its self-test).

Log volume per host varies by 10-20x depending on what's actually
logging (quiet baseline auth/cron/systemd vs. verbose auditd, SSSD
debug, or applications routed through syslog) — industry benchmarks
for "a typical Linux server" range from ~10MB/day to 300MB+/day, which
is too wide a spread to size real disks against. Your lab's `admin1`
already has real fleet traffic flowing through it, so pull real numbers
instead of using the ballpark table further down.

**1. Flat-file volume per host, per day** (let it run a full day first):

```bash
# Total across every collected host right now
sudo du -sch /var/log/remote/*/ | tail -1

# Per host, sorted biggest first — spot any one noisy host skewing the average
sudo du -sh /var/log/remote/*/ | sort -rh | head -20

# How many days of data is currently in there (oldest file vs now)
sudo find /var/log/remote -name '*.log' -printf '%T@ %p\n' | sort -n | head -1
```

Divide the total by (number of hosts × number of days covered) to get
**MB/host/day**.

**2. PostgreSQL database size and how many days it represents:**

```bash
sudo -u postgres psql -d syslog -c "SELECT pg_size_pretty(pg_database_size('syslog'));"

# Row count and the actual date span currently stored
sudo -u postgres psql -d syslog -c "SELECT count(*), min(receivedat), max(receivedat) FROM systemevents;"

# Breakdown: how much of that is the trigram search index specifically
sudo -u postgres psql -d syslog -c "\di+ idx_systemevents_message_trgm"
sudo -u postgres psql -d syslog -c "SELECT pg_size_pretty(pg_total_relation_size('systemevents'));"
```

`pg_total_relation_size` includes the table plus all its indexes —
that's the number to divide by (days covered × host count) to get your
real **DB MB/host/day**, including index overhead.

**3. Extrapolate to your real fleet size:**

```
Fleet /var/log/remote (14-day retention) ≈ (lab MB/host/day) × N × ~3.2
    — where N is your target fleet size
    — the ×3.2 accounts for 1 day uncompressed + 13 days gzip'd at ~6:1

Fleet PostgreSQL (90-day retention) ≈ (lab DB MB/host/day) × N × 90
```

Do this before buying/provisioning disk — at larger fleet sizes the gap between
a "quiet" and "chatty" estate is easily a 5-10x difference in required
PostgreSQL storage, dwarfing every other choice in this repo.

## Worked example (5-host home lab, ~3-day sample)

Real measurement from `admin1`, 2026-09-01 — useful as a template for
running your own numbers, and as a caution about small-sample bias:

```
/var/log/remote: 158M total, 5 hosts, oldest file 2026-08-29 09:52,
                 newest 2026-09-01 20:16 (3.43 days)
  → 158 / 5 / 3.43 ≈ 9.2 MB/host/day

PostgreSQL (pg_total_relation_size, so trigram index already included):
                 463MB, 5 hosts, oldest row 2026-08-29 17:22,
                 newest row 2026-09-01 20:16 (3.12 days)
  → 463 / 5 / 3.12 ≈ 29.7 MB/host/day
```

Extrapolated to an illustrative 100-host fleet: `/var/log/remote`
@ 14 days ≈ **9.2 × 100 × 3.2 ≈ 2.9 GB**; PostgreSQL @ 90 days ≈
**29.7 × 100 × 90 ≈ 261 GB**. Notably quieter than the ballpark table
below — this lab genuinely has low traffic.

**Don't provision off the bare extrapolation.** A 3-day sample misses
weekly logrotate/cron reports and monthly patch cycles (this repo's own
`system_update`/`dnf_history` runs add volume during patch windows),
and real production servers with real users, applications, and security
tooling will almost certainly be noisier than 5 quiet lab boxes. Build
in real margin — e.g. provision PostgreSQL at roughly 2x the
extrapolated figure, with more free space left in the volume group
beyond that — and re-run this same measurement once you've piloted
against a handful of real hosts for at least a week, ideally
spanning one patch cycle, before treating any number as final.

## Ballpark table (only if you can't measure first)

Use this only as a sanity check on your own lab numbers, not as a
substitute for them:

| Logging profile                                                              | Per host/day | Fleet/day (per 100 hosts) | `/var/log/remote` @ 14d | PostgreSQL @ 90d      |
| ------------------------------------------------------------------------------ | -----------: | ---------------------: | -----------------------: | ----------------------: |
| **Low** — quiet baseline (auth, cron, systemd, sshd only)                     |     ~20 MB   |         ~2.0 GB        |          ~10-13 GB        |       ~400-600 GB       |
| **Medium** — typical mixed estate (SELinux enforcing, moderate sudo/auditd)   |     ~75 MB   |         ~7.5 GB        |          ~30-45 GB        |       ~1.2-1.9 TB        |
| **High** — verbose (heavy auditd, SSSD debug, app logs via syslog)            |    ~200 MB   |         ~20 GB         |          ~95-125 GB      |       ~3-4.5 TB          |

`/linuxdb` (reports + config snapshots + hostinfo history) is
negligible by comparison — plain text, a handful of KB per host per
run, capped by `keep_runs`. Budget **10-20 GB** for it even on large fleets
with default retention and move on.

**Given how large PostgreSQL gets at 90-day retention on the full raw
stream, it's worth deciding — after you have your own real numbers —
whether 90 days of everything, full-text-indexed, is actually what you
want fleet-wide, versus e.g. lowering `log_db_retention_days` for a
pilot, or accepting the larger number if 90-day searchable history is
a real requirement. That's your call to make with real figures in
hand, not something to lock in from this table.**

## Recommended LVM layout

Three separate LVs, one per data path above, on the collector host —
independent growth, and a bad estimate on one doesn't starve the
others. Assumes a spare disk/SAN LUN presented to the host (adjust the
device name after checking `lsblk`).

**Do this before your first `ansible-playbook` run against this host.**
`roles/log_db` runs `postgresql-setup --initdb` the first time it sees
no existing `/var/lib/pgsql/data`, and the `postgresql-server` RPM's
own install scriptlet sets ownership on `/var/lib/pgsql` — both need to
land on your LV, not on the root filesystem, so mount before you
install anything or run the playbook.

```bash
# 0. Confirm the new disk and its device name
lsblk

# 1. Physical volume + volume group
sudo pvcreate /dev/sdb
sudo vgcreate vg_logcollector /dev/sdb

# 2. Logical volumes — replace these sizes with YOUR measured numbers
#    from the section above (example below reflects a lab-measured,
#    ~2x-margin starting point — see "Worked example"). Leave
#    meaningful free space unallocated in the VG (don't use -l 100%FREE)
#    so you can lvextend later without adding another disk.
sudo lvcreate -L 25G   -n lv_var_log_remote vg_logcollector
sudo lvcreate -L 900G  -n lv_pgsql_data     vg_logcollector
sudo lvcreate -L 20G   -n lv_linuxdb        vg_logcollector

sudo vgs vg_logcollector   # confirm free space remains for future growth

# 3. Format — XFS is the RHEL 8/9/10 default and what this repo assumes
sudo mkfs.xfs /dev/vg_logcollector/lv_var_log_remote
sudo mkfs.xfs /dev/vg_logcollector/lv_pgsql_data
sudo mkfs.xfs /dev/vg_logcollector/lv_linuxdb

# 4. Mount points matching this repo's defaults exactly
sudo mkdir -p /var/log/remote /var/lib/pgsql /linuxdb

# 5. /etc/fstab — use blkid UUIDs if you prefer over /dev/mapper paths
sudo tee -a /etc/fstab <<'EOF'
/dev/mapper/vg_logcollector-lv_var_log_remote  /var/log/remote  xfs  defaults  0 0
/dev/mapper/vg_logcollector-lv_pgsql_data      /var/lib/pgsql   xfs  defaults  0 0
/dev/mapper/vg_logcollector-lv_linuxdb         /linuxdb         xfs  defaults  0 0
EOF

sudo mount -a
df -hT /var/log/remote /var/lib/pgsql /linuxdb
```

After the first real playbook run, sanity-check ownership landed
correctly on the PostgreSQL mount (should be `postgres:postgres`, not
`root:root`):

```bash
ls -ld /var/lib/pgsql /var/lib/pgsql/data
```

If it didn't take (rare, but possible depending on install order),
`sudo chown postgres:postgres /var/lib/pgsql` and re-run
`ansible-playbook playbooks/centralized_logging.yml --tags log_db --limit <collector-host>`.

### Single shared VG vs. one VG per path — pick deliberately

The layout above — one `vg_logcollector` VG holding all three LVs — is
the **recommended layout when growth is still uncertain**, and the reason is
free-space pooling, not just tidiness: real per-host log volume is
genuinely uncertain until the "Post-pilot sizing checklist" below
has run against real hosts for at least a week, so whichever of
the three paths turns out to grow faster than expected can be
`lvextend`ed from the VG's spare capacity without touching hardware or
waiting on a new disk/LUN from your storage team. Provision the VG
with real slack left unallocated (see the `-l 100%FREE` warning in step
2 above) to actually get this benefit — a shared VG with zero free
space gives you nothing extra over three separate ones.

The home lab (`admin1`) ended up on a **different, equally valid**
pattern instead: three independent single-PV VGs (`vg_logs`,
`vg_linuxdb`, `vg_pgsql`), one per NVMe partition, each fully allocated
to its one LV. This trades away the shared-growth-pool flexibility for
slightly stronger isolation (a problem with one VG's metadata can't
touch the others) — a reasonable tradeoff for a lab where sizing is
already well understood from real measurements and hardware is cheap
to add. If your storage team provisions separate disks/LUNs per
path rather than one large disk, you can still get the pooling benefit
by adding all of them as PVs into one shared VG
(`vgcreate vg_logcollector /dev/sdb /dev/sdc /dev/sdd`) rather than
creating one VG per disk — the pooling benefit comes from the VG
boundary, not from how many physical disks back it.

Either pattern satisfies the "three separate filesystems" requirement
in the README. Don't feel obligated to make an existing, working
deployment match the other pattern just for naming consistency —
choose per deployment based on whether growth flexibility or
isolation matters more there.

## Migrating an already-populated host to dedicated filesystems

Everything in "Recommended LVM layout" above assumes a brand-new host,
provisioned before the first `ansible-playbook` run. If `/var/log/remote`,
`/var/lib/pgsql`, and/or `/linuxdb` are already live on a collector's
root filesystem — the common case on a lab or PoC host that was stood
up before this requirement was written down — use this section instead
to move the existing data onto dedicated LVs **without losing it**.

**Do this one path at a time, not all three in one sitting**, and
confirm each one is healthy before starting the next.

### The one mistake that loses no data but hides it

If you mount a freshly formatted filesystem directly on top of a
directory that already has files in it (e.g. `mount ... /var/lib/pgsql`
while `/var/lib/pgsql/data` already exists), the old files aren't
deleted — they're just **hidden underneath the new mount** and keep
consuming root filesystem space invisibly until something unmounts the
new filesystem again. Every procedure below avoids this deliberately:
copy data onto the new filesystem first, mounted at a **temporary**
path, then swap the real mount point in only after the copy is
confirmed complete.

### 0. Provision the new volume(s) first

Follow steps 0-3 of "Recommended LVM layout" above (`pvcreate` /
`vgcreate` / `lvcreate` / `mkfs.xfs`) to create and format whichever of
`lv_var_log_remote`, `lv_pgsql_data`, `lv_linuxdb` you're migrating —
sized from your own measurements in "Do this first" above. **Stop
before the `mkdir -p` / `fstab` / `mount -a` steps** — this section
replaces those with a copy-then-swap sequence instead of a plain mount
onto empty directories.

### 1. Migrate `/var/lib/pgsql` (PostgreSQL data — do this one first, it's the one that matters most)

```bash
# 1a. Mount the new LV at a temporary path
sudo mkdir -p /mnt/pgsql_new
sudo mount /dev/vg_logcollector/lv_pgsql_data /mnt/pgsql_new

# 1b. Stop everything that talks to the database, so nothing writes
#     mid-copy and nothing logs noisy connection errors during the
#     maintenance window (same units as ROLLBACK_RUNBOOK.md Phase 1 —
#     reuse that list if you have it open):
sudo systemctl stop \
  postgresql \
  log-search-ui.service \
  service-failure-watcher.timer \
  host-availability-watcher.timer \
  disk-space-watcher.timer \
  chrony-watcher.timer \
  linuxdb-dashboard.service

# 1c. Copy everything, preserving ownership/permissions/xattrs
#     (postgres:postgres ownership matters here — -a alone covers
#     owner/group/perms/timestamps; -X/-A add xattrs/ACLs if you use
#     them, harmless if you don't)
sudo rsync -aXAv --info=progress2 /var/lib/pgsql/ /mnt/pgsql_new/

# 1d. Move the old directory aside (don't delete yet) and swap in the
#     empty mount point at the real path
sudo umount /mnt/pgsql_new
sudo mv /var/lib/pgsql /var/lib/pgsql.bak
sudo mkdir /var/lib/pgsql

# 1e. Point fstab at the real path (not the temporary one) and mount
sudo tee -a /etc/fstab <<'EOF'
/dev/mapper/vg_logcollector-lv_pgsql_data      /var/lib/pgsql   xfs  defaults  0 0
EOF
sudo mount -a

# 1f. Verify before starting anything back up
df -hT /var/lib/pgsql
ls -ld /var/lib/pgsql /var/lib/pgsql/data   # must be owned by postgres:postgres
sudo -u postgres ls /var/lib/pgsql/data/PG_VERSION

# 1g. Start the database back up and confirm the data is really there
sudo systemctl start postgresql
sudo -u postgres psql -d syslog -c "SELECT count(*), min(receivedat), max(receivedat) FROM systemevents;"
# ^ compare this row count/date range against what you saw BEFORE the
#   migration (re-run the same query beforehand if you didn't already
#   have a number to compare against)

# 1h. Once confirmed, restart the consumers stopped in 1b
sudo systemctl start \
  log-search-ui.service \
  service-failure-watcher.timer \
  host-availability-watcher.timer \
  disk-space-watcher.timer \
  chrony-watcher.timer \
  linuxdb-dashboard.service
```

**Only after you're confident the migration is good** (give it a real
day of traffic, not just a clean `SELECT count(*)`), reclaim the root
filesystem space:

```bash
sudo rm -rf /var/lib/pgsql.bak
```

### 2. Migrate `/var/log/remote` (raw flat-file logs)

Same pattern, simpler — plain `root:root` files, one service
(`rsyslog`) to pause:

```bash
sudo mkdir -p /mnt/remote_new
sudo mount /dev/vg_logcollector/lv_var_log_remote /mnt/remote_new

sudo systemctl stop rsyslog
sudo rsync -aXAv --info=progress2 /var/log/remote/ /mnt/remote_new/

sudo umount /mnt/remote_new
sudo mv /var/log/remote /var/log/remote.bak
sudo mkdir /var/log/remote

sudo tee -a /etc/fstab <<'EOF'
/dev/mapper/vg_logcollector-lv_var_log_remote  /var/log/remote  xfs  defaults  0 0
EOF
sudo mount -a

df -hT /var/log/remote
ls -ld /var/log/remote   # must be root:root, mode 0750
ls /var/log/remote | head   # per-host subdirectories should all be present

sudo systemctl start rsyslog
# Confirm new messages are still landing (give it a minute, then):
sudo find /var/log/remote -name '*.log' -newermt '-2 minutes'
```

Once confirmed: `sudo rm -rf /var/log/remote.bak`.

### 3. Migrate `/linuxdb` (reports, config snapshots, hostinfo history)

No service to stop here — these three roles only write during an
actual `ansible-playbook` run against `linuxdb_dashboard.yml` or
`hostinfo.yml`, so just make sure no such run is in flight (check
`ps aux | grep ansible-playbook` or your cron log), then:

```bash
sudo mkdir -p /mnt/linuxdb_new
sudo mount /dev/vg_logcollector/lv_linuxdb /mnt/linuxdb_new

sudo rsync -aXAv --info=progress2 /linuxdb/ /mnt/linuxdb_new/

sudo umount /mnt/linuxdb_new
sudo mv /linuxdb /linuxdb.bak
sudo mkdir /linuxdb

sudo tee -a /etc/fstab <<'EOF'
/dev/mapper/vg_logcollector-lv_linuxdb         /linuxdb         xfs  defaults  0 0
EOF
sudo mount -a

df -hT /linuxdb
ls -ld /linuxdb   # ownership should match whichever account normally
                  # runs ansible-playbook here (these roles' file tasks
                  # have no explicit owner/become, so they inherit
                  # whatever ran the play — root if run as root,
                  # svc_ansible if that's your service account)
ls /linuxdb   # reports/ configs/ hostinfo/ should all be present
```

Verify the dashboard still reads correctly (`http://<collector>:8082/`
or `https://` if TLS is on), then reclaim space:
`sudo rm -rf /linuxdb.bak`.

### After all three are migrated

```bash
df -hT /var/log/remote /var/lib/pgsql /linuxdb
```

All three should show their dedicated `/dev/mapper/vg_logcollector-*`
devices, not the root filesystem. Re-run the "Ongoing monitoring"
section's `df_check.sh`-backed dashboard check to confirm it now
reports the correct per-mount usage for each of the three, not one
combined root-filesystem number.

## Growing a volume later

If your real fleet volume runs ahead of what you provisioned — expected
if the ballpark table above was your only input — extend in place with
no downtime, assuming free space remains in the VG (or add a new PV to
the VG first with `vgextend`):

```bash
sudo lvextend -L +500G /dev/vg_logcollector/lv_pgsql_data
sudo xfs_growfs /var/lib/pgsql
```

`xfs_growfs` (not `resize2fs`) is correct here because step 3 above
formatted these as XFS, RHEL's default.

## Ongoing monitoring

`roles/linuxdb_reports`' `df_check.sh` already flags any filesystem
≥85% used (see `roles/linuxdb_reports/README.md` "Disk & Filesystem")
and surfaces it on the `linuxdb_dashboard` web UI per host — once
that's running against the collector host itself, you'll get an early
warning on any of these three mounts filling up faster than expected,
without having to remember to check manually.

## Post-pilot sizing checklist

Run this once a handful of **real production hosts** have been
forwarding logs for at least a week, and before you widen the rollout
to the rest of the fleet. This is the point where you replace the
lab-derived numbers above with real production numbers, and it's the
last natural checkpoint before committing to a storage purchase or a
full-fleet rollout.

- [ ] **Confirm the sample window is long enough.** At least 7 days,
      ideally spanning one weekly cron/report cycle and, if timing
      allows, one patch window (`system_update` runs add volume that a
      shorter window won't show).
      ```bash
      # How many pilot hosts, and the real date span covered
      ls -d /var/log/remote/*/ | wc -l
      sudo find /var/log/remote -type f -printf '%TY-%Tm-%Td %TH:%TM  %p\n' | sort | head -1
      sudo find /var/log/remote -type f -printf '%TY-%Tm-%Td %TH:%TM  %p\n' | sort | tail -1
      ```
- [ ] **Re-measure flat-file and PostgreSQL volume against the pilot
      hosts**, using the exact same commands as "Do this first" above:
      ```bash
      sudo du -sch /var/log/remote/*/ | tail -1
      sudo -u postgres psql -d syslog -c "SELECT pg_size_pretty(pg_database_size('syslog'));"
      sudo -u postgres psql -d syslog -c "SELECT pg_size_pretty(pg_total_relation_size('systemevents'));"
      sudo -u postgres psql -d syslog -c "SELECT count(*), min(receivedat), max(receivedat) FROM systemevents;"
      ```
- [ ] **Recompute MB/host/day for both**, and compare against the lab
      baseline (9.2 MB/host/day flat-file, 29.7 MB/host/day PostgreSQL
      — see "Worked example" above). Real hosts with real users,
      applications, and security tooling are expected to run higher;
      note the actual multiplier you observe rather than assuming one.
- [ ] **Recompute the fleet-size extrapolation** for your real
      target fleet size with the pilot-measured rate:
      ```
      Fleet /var/log/remote @ 14d ≈ (pilot MB/host/day) × <fleet size> × 3.2
      Fleet PostgreSQL @ 90d      ≈ (pilot MB/host/day) × <fleet size> × 90
      ```
- [ ] **Compare against what's already provisioned** (the `vg_logcollector`
      LVs from "Recommended LVM layout" above). If the new extrapolation
      — with margin — exceeds current LV size, `lvextend` +
      `xfs_growfs` now, before widening the rollout, not after a mount
      fills up mid-rollout (see "Growing a volume later" above).
      ```bash
      sudo vgs vg_logcollector   # confirm enough free space remains
      ```
- [ ] **Revisit `log_db_retention_days` and
      `rsyslog_remote_log_retain_days`** if the real PostgreSQL number
      is dramatically larger than planned — lowering retention is a
      one-line `group_vars` change and may be more practical than
      continuing to grow the volume, depending on how much searchable
      history you actually need.
- [ ] **Confirm the retention cleanup timer is actually deleting old
      rows**, not just installed — a pilot running for a week won't
      yet have hit the 90-day PostgreSQL cutoff, but the flat-file
      14-day `logrotate` cutoff should already have fired at least
      once:
      ```bash
      sudo systemctl status log-db-retention-cleanup.timer
      sudo logrotate -d /etc/logrotate.d/remote-logs   # dry-run, confirms the rule parses and would fire
      ```
- [ ] **Write down the final numbers** you're taking into the full
      rollout (pilot MB/host/day, fleet extrapolation, chosen LV sizes)
      somewhere durable — a comment in `group_vars`, a line in your own
      change-ticket, or an update to this file — so the next person
      (including future you) knows these were measured, not guessed.
