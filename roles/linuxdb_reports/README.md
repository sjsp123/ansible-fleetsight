# Role: linuxdb_reports

## Purpose

Runs five read-only diagnostic scripts against every host in the play —
`df_check.sh`, `proc_check.sh`, `svr_check.sh`, `system_stats.sh`,
`dnf_updates.sh` — and saves each one's raw output back on the
**control node** (the host running `ansible-playbook`, normally
`admin1`) as a timestamped file per host per check. This is the data
source `roles/linuxdb_dashboard` reads from to render the web
dashboard's drill-down pages.

These four scripts started out in this repo as a separate `scripts/*.sh`
copy, run ad hoc via `playbooks/push_script_and_run.yml`, which
previously only printed their output live during the playbook run via
`debug` — nothing was ever saved, so there was no history and nothing
for a dashboard to read. This role was the fix: the scripts were copied
into `roles/linuxdb_reports/files/` so the role is self-contained, and
every run's output is now captured and kept.

That left two copies of the same four scripts, which quietly diverged
over time — `scripts/*.sh` kept an older, pre-dashboard set of checks
(including a few, like cluster/multipath/zombie-count detection, that
were later deliberately dropped fleet-wide for being low-signal) while
`roles/linuxdb_reports/files/*.sh` kept evolving. `scripts/*.sh` has
since been removed entirely to close that gap: `roles/linuxdb_reports/
files/` is now the single source, used both by this role **and** by the
ad hoc `playbooks/push_script_and_run.yml` path (see the root-level
`ansible_df_check.sh` / `ansible_proc_check.sh` / `ansible_svr_check.sh`
/ `ansible_system_stats.sh` wrapper one-liners, which now point
`push_script_src` at `roles/linuxdb_reports/files/<script>.sh`). One
consequence: an ad hoc run no longer repeats the OS/kernel/uptime/
filesystem-%/NTP/SELinux/reboot-required/MFR-MODEL-SERIAL facts that
`svr_check.sh`/`system_stats.sh` intentionally leave out because
`roles/hostinfo`'s Overview table already shows them — for a fleet
host, run `playbooks/hostinfo.yml` (or check the dashboard) alongside
an ad hoc push if you want those static facts too.

Nothing is installed or left behind on the managed hosts themselves —
each script is copied to a scratch path, run, and removed again
immediately after. All state lives on the control node.

## Behaviour

- Copies each check script to `linuxdb_reports_remote_tmp` on the managed
  host, runs it, captures stdout + exit code, then deletes it again.
- On the control node, writes:
  ```
  {{ linuxdb_reports_data_dir }}/<hostname>/<check_key>/<timestamp>.txt   (raw stdout)
  {{ linuxdb_reports_data_dir }}/<hostname>/<check_key>/<timestamp>.rc    (exit code)
  {{ linuxdb_reports_data_dir }}/<hostname>/<check_key>/latest            (timestamp of the most recent run)
  ```
  `<timestamp>` is `YYYYMMDD_HHMMSS`, generated per host at the start of
  its run, so filenames sort chronologically.
- A check script exiting non-zero (e.g. `df_check.sh` exits 1 when a
  filesystem is over its warning threshold, 2 when over its critical
  threshold) is a **result to report**, not an ansible failure — the
  play never stops because a check found something worth flagging. The
  exit code is saved alongside the output so the dashboard can show it
  — and, for `df_check.sh` specifically, feeds the Overview page's own
  traffic light too (see "What each tab shows" below and
  `roles/linuxdb_dashboard/README.md`).
- After each run, older timestamped files beyond `linuxdb_reports_keep_runs`
  are pruned automatically, per host per check.
- `system_stats.sh`'s public-IP lookup (`curl checkip.dyndns.org`) has
  `--connect-timeout 2 --max-time 3` and a fallback string, so a lab host
  with no internet egress fails that one field fast instead of hanging
  the whole collection run. It's also gated by a plain `CHECK_PUBLIC_IP`
  variable near the top of the script (`false` by default) - many
  networks route outbound HTTP through a proxy/firewall (e.g. Zscaler)
  that intercepts or otherwise interferes with the request, which is
  unwanted noise in a routine report even with the short timeouts above.
  Set it to `true` directly in the script to re-enable; `PRIVATE_IPS`
  (local interface addresses) always runs either way.

## What each tab shows

- **Disk & Filesystem** (`df_check.sh`) — lists **every** filesystem `df`
  reports (tmpfs/devtmpfs/squashfs/efivarfs/iso9660/etc included —
  earlier versions filtered these out, current version doesn't), plus a
  two-tier utilisation-warning section and an fstab-vs-mounted
  reconciliation check.
  - **Thresholds**: `WARN_THRESHOLD=85`, `CRIT_THRESHOLD=95` (percent
    used), both plain variables at the top of the script — edit them
    directly if 85/95 doesn't fit a given fleet. WARNING is meant as
    "getting tight, worth a look" (log rotation/cleanup usually still
    recovers it); CRITICAL is meant as "genuinely close to full" (a
    busy `/var/log` or package cache can eat the last few percent
    fast). The utilisation check itself **excludes** pseudo/virtual
    filesystem types (`tmpfs`, `devtmpfs`, `squashfs`, `overlay`,
    `iso9660`, `efivarfs`) — those are normally near-100%-full by
    design (RAM-backed or fixed-size), so flagging them would just be
    noise; they still appear in the full utilisation table above, just
    never trigger a WARNING/CRITICAL line. The percentage is coerced
    to a genuine number (`pct + 0`) right before the threshold
    compare — `awk` strips a value's "numeric string" typing the
    moment you run `gsub()` on it (stripping the `%` sign here), so
    without the explicit coercion the compare silently falls back to
    a plain string comparison and, e.g., a clean `9%` filesystem could
    lexicographically outrank `85` (`"9" >= "85"` is true as strings,
    since `9` sorts after `8`) and get flagged as a false WARNING.
  - **Exit code**: `0`=PASS, `1`=WARNING, `2`=CRITICAL — the worst of
    the utilisation check above and the fstab reconciliation below (a
    mount/fstab mismatch alone is WARNING, it can't push this to
    CRITICAL on its own). This is the same rc the dashboard reads to
    colour the per-check tab (`rc_class()` in
    `roles/linuxdb_dashboard/files/app.py`), and — as of this check's
    two-tier update — the same rc that now also feeds the Overview
    page's traffic-light badge for this host (see
    `roles/linuxdb_dashboard/README.md`'s "Traffic-light status badge"
    section): a host with perfectly clean `hostinfo.csv` columns can
    still show `WARN`/`CRIT` on the Overview page purely because of
    its latest Disk & Filesystem result.
  - The fstab-vs-mounted reconciliation section stays scoped to "real"
    filesystem types on both sides (pseudo mounts like tmpfs/proc
    rarely have a clean 1:1 fstab entry, so including them there would
    just produce noisy false mismatches) — only the display and
    utilisation-warning sections show everything.
  - The reconciliation also excludes autofs-triggered submounts (e.g.
    a user's NFS home directory under `/home`, live only while they're
    logged in) — these never have their own `/etc/fstab` entry by
    design, they're managed by autofs maps (`/etc/auto.master`,
    `/etc/auto.home`, etc.) instead, so flagging their absence from
    fstab would be a false positive. Detected generically at runtime
    (`findmnt -t autofs` for the currently-active base mountpoints,
    e.g. `/home`), not hardcoded to any specific path.
- **Processes** (`proc_check.sh`) — non-system-account processes, plus
  top CPU/memory consumers. Unchanged this round.
- **Server Health** (`svr_check.sh`) — live/dynamic operational signals
  only. OS/kernel/arch, uptime/last-boot, static filesystem %, NTP sync
  (binary flag), SELinux mode, reboot-required and MFR/MODEL/SERIAL are
  all deliberately excluded — they're already on the fleet Overview
  table or the Disk & Filesystem tab. Cluster/multipath detection,
  zombie process count and both prior service up/down checks were
  removed (see "Services" below); the check set was rewritten around a
  curated list grounded in Red Hat's own documentation and common
  industry server-health practice (sources listed under "Sources
  referenced" below) rather than the previous ad hoc set. Every
  threshold lives as a plain variable at the top of the script — edit
  directly if a number doesn't fit a given fleet.
  - **Resource pressure**:
    - `LOAD_AVG` — 1/5/15-minute load, now normalised against `nproc`
      (`LOAD_WARN_RATIO=1.0`, `LOAD_CRIT_RATIO=2.0` — 1-minute load
      per core). Previously reported the raw numbers only with no
      PASS/WARNING/CRITICAL evaluation at all.
    - `MEMORY` — % used, now computed from `/proc/meminfo`'s kernel-
      calculated `MemAvailable`, not a naive `free`(1) "used" column —
      reclaimable page cache/buffers no longer produce false alarms on
      an otherwise healthy box (`MEM_WARN_THRESHOLD=80`,
      `MEM_CRIT_THRESHOLD=90`, unchanged from before).
    - `SWAP_USAGE` — % of configured swap used (`SWAP_WARN_THRESHOLD=50`,
      unchanged).
    - `SWAP_ACTIVITY` (NEW) — actual paging happening right now
      (`vmstat`'s `si`/`so` KB/s from a live sample), distinct from
      static swap-%-used: a box can sit at 60% swap used but be
      completely idle, or at 5% swap used but actively thrashing.
      WARNING on any nonzero si/so in the sampled second.
    - `CPU_IOWAIT` / `CPU_UTIL` / `CPU_STEAL` — all three now come from
      one shared `vmstat` sample (previously iowait only, INFO-only).
      `CPU_UTIL` (100 − idle) reports actual busy-ness that load
      average alone can't (`CPU_UTIL_WARN_THRESHOLD=85`,
      `CPU_UTIL_CRIT_THRESHOLD=95`). `CPU_STEAL` (NEW) — VM-only signal
      for hypervisor oversubscription: time this vCPU wanted to run but
      the host gave the physical core to a different guest instead
      (`CPU_STEAL_WARN_THRESHOLD=5`, `CPU_STEAL_CRIT_THRESHOLD=10`,
      following common virtualisation-monitoring guidance — see
      Sources). Reports INFO/not-applicable on bare metal, where
      `vmstat` has no `st` column. `IOWAIT_WARN_THRESHOLD=20`,
      `IOWAIT_CRIT_THRESHOLD=40` unchanged.
    - `INODE_USAGE` — unchanged, flags any filesystem >=85% inodes used
      even when block-space still looks fine (`INODE_WARN_THRESHOLD=85`).
  - **Disk performance & storage health** (all NEW):
    - `DISK_IO` — worst-device `%util`/`await` from `iostat -xz`
      (`sysstat` package — confirmed BaseOS/AppStream-provenance, see
      Sources), two 1-second samples with only the second (real-time)
      sample used. `DISK_UTIL_WARN_THRESHOLD=80`,
      `DISK_UTIL_CRIT_THRESHOLD=95`. INFO if `iostat` isn't installed.
    - `FS_READONLY` — the classic silent RHEL failure mode: after a
      disk I/O error the kernel remounts a filesystem read-only as a
      data-loss safety net, with everything else still looking "up".
      Scoped to local block-device-backed types (`xfs`/`ext4`/`ext3`/
      `ext2`/`btrfs`/`vfat`); network filesystems are excluded since a
      read-only NFS export is often an intentional server-side choice.
      `RO_EXCLUDE_MOUNTS` (top of script, empty by default) can list
      mountpoints your own hardening baseline deliberately mounts `ro`
      (e.g. a CIS profile that mounts `/boot` read-only). CRITICAL if
      any unexpected read-only mount is found.
    - `LVM_THINPOOL` — thin-pool data/metadata % full via `lvs`
      (`lvm2` package). A thin pool can run out of backing space while
      every filesystem on top still reports plenty of free room
      (thin provisioning over-commits by design) — this is invisible
      to ordinary df-based capacity monitoring.
      `LVM_THINPOOL_WARN_THRESHOLD=80`,
      `LVM_THINPOOL_CRIT_THRESHOLD=90`. INFO if `lvs`
      isn't installed or no thin pools are in use (plain, non-thin LVs
      are intentionally not flagged here — already covered by ordinary
      filesystem monitoring).
    - `RAID_STATUS` — Linux software RAID (`/proc/mdstat`) degradation
      only; hardware RAID controllers need vendor tooling (MegaCli/
      storcli/ssacli/perccli) that isn't a standard BaseOS/AppStream
      package, so is out of scope. CRITICAL if any array shows a
      missing member (e.g. `[UU_U]`). INFO if no md arrays exist.
      **Multipath is deliberately still not covered** — already
      dropped previously as low-signal for this fleet (no SAN-attached
      hosts); flag if you'd like this reconsidered.
  - **Services**: none in the traditional up/down sense — intentionally
    removed entirely and never reintroduced by this update. This
    report previously carried two service checks (a failed-units
    count/breakdown, and a five-daemon `sshd`/`firewalld`/`chronyd`/
    `auditd`/`crond` up/down check), both dropped because they
    duplicated signal living on a *different* playbook/schedule — the
    failed-units count on the fleet Overview table (`FAILED_SVC_CNT`/
    `FAILED_SVCS`, via `roles/hostinfo`) and `roles/service_failure_watcher`'s
    mail alert, and (partially, for chronyd) the Overview table's
    `CHRONYD` column. Service-status monitoring lives in exactly one
    place: the Overview table (`roles/hostinfo`,
    `group_vars/all/host_checks.yml`) for daemon up/down state, plus
    `service_failure_watcher`'s email for crashed units. This update
    adds one narrow, deliberately non-duplicating addition instead:
    - `SVC_RESTARTS_<NAME>` (NEW) — cumulative-since-boot restart
      count (`systemctl show <unit> -p NRestarts`) for the same six
      daemons already tracked elsewhere (`sshd`/`firewalld`/`chronyd`/
      `auditd`/`crond`/`sssd`), one row per daemon.
      `SERVICE_RESTART_WARN_THRESHOLD=3`. This is a different signal
      to "is it up right now" — a flapping service that keeps crashing
      and getting auto-restarted by systemd never shows as "failed"
      at any single point-in-time check, but a high restart count
      reveals it. INFO if the unit doesn't exist on that host.
  - **Logs**: recent OOM-killer events (`journalctl -k`, 24h lookback,
    CRITICAL if any — regex broadened to also catch cgroup-scoped OOM
    events, e.g. `systemd-oomd` or a container hitting its own
    `memory.max`, not just classic system-wide kernel OOM kills) and
    recent kernel/hardware errors (`journalctl -k -p err`, 6h lookback,
    WARNING if any). Two more log-based checks are NEW this round:
    - `SELINUX_AVC_6H` (NEW) — actual AVC denials in the last 6h via
      `ausearch -m avc` (`audit` package). Overview's `SELINUX` column
      only shows the static enforcing/permissive mode; this is the
      dynamic signal that mode alone can't show — is SELinux actually
      blocking something right now. The 6-hour window matches this
      report's own cron cadence so there's no gap or double-count
      between runs. WARNING if any denials found; INFO if `ausearch`
      isn't installed or SELinux is disabled.
    - `CRASH_DUMPS` (NEW) — non-empty `/var/crash` is direct evidence a
      kernel panic happened at some point, worth surfacing even if the
      host has been up and quiet since. WARNING if any dump files are
      present; INFO if `/var/crash` doesn't exist.
  - **Process & network**: processes stuck in D-state/uninterruptible
    sleep (a much more actionable hung-I/O signal than the old zombie
    count, especially relevant given NFS mounts in this lab), and
    network interface RX/TX errors, drops and overruns per non-loopback
    interface (`ip -s link show`; overrun counters were added this
    round alongside the existing error/drop counters). These counters
    are cumulative since the interface was last brought up (usually
    since boot) and don't reset on their own, so a single historical
    drop (e.g. one packet during boot-time link negotiation - confirmed
    on `idm1`'s `enp2s0` during testing) would otherwise pin this check
    to permanent WARNING. NET_ERR_WARN_THRESHOLD (default 10, combined
    RX+TX errors+drops+overruns per interface) absorbs that noise; the
    exact counts for any nonzero interface are still shown even when
    below threshold, only the PASS/WARNING classification is gated on
    it. `NET_CARRIER` (NEW) — flags an administratively-up interface
    reporting no carrier (cable/switch-port down), read directly from
    each interface's `/sys/class/net/<if>/operstate` and `flags` rather
    than parsing `ip link show` text. `SOCKET_HEALTH` (NEW) — total TCP
    socket count from `ss -s` (`iproute` package — Red Hat's own
    recommended replacement for `netstat`, see Sources), a coarse but
    cheap signal for socket exhaustion (leaking connection pool,
    runaway short-lived-connection churn, a service stuck accumulating
    CLOSE_WAIT sockets). `SOCKET_WARN_THRESHOLD=5000`. Per-state
    breakdown (TIME_WAIT/CLOSE_WAIT counts, retransmit rates) needs
    deeper, more kernel-version-fragile `/proc/net/netstat` parsing —
    kept out of this pass in favour of the one number most directly
    tied to real exhaustion risk.
  - **Authentication & Identity**: failed SSH logins and failed `sudo`
    attempts in the last 24h (via `journalctl`), locked local accounts
    (real human UID >=1000 accounts only — **does not cover IdM/domain
    account lockouts**, since those are tracked by SSSD/pam_faillock
    rather than `/etc/shadow` and the on-disk format has changed across
    the RHEL 7->8->9 pam_tally2->pam_faillock transition, too fragile to
    parse reliably fleet-wide), live SSSD daemon status (distinct from
    the static REALM name on Overview), IdM/LDAP domain online/offline
    status (`sssctl domain-status`, only runs on domain-joined hosts —
    reports INFO "Not domain-joined" otherwise), Kerberos/KDC
    reachability (a real `kinit` round-trip using the host's own
    keytab, ticket destroyed immediately after — only runs where
    `/etc/krb5.keytab` and `krb5-workstation` are present), the host's
    own DNS self-resolution (`getent hosts $(hostname -f)`), and a full
    certmonger certificate inventory (`getcert list`) — **every**
    tracked certificate gets its own row (not just ones close to
    expiry), labelled by nickname, falling back to the subject CN
    (disambiguated with the certificate file's basename when two
    FILE-backed certs share the same CN — confirmed necessary on
    `idm1`, where the httpd cert and the KDC cert both have subject
    `CN=idm1.lab.example.com`; they now render as "idm1.lab.example.com
    (httpd.crt)" and "idm1.lab.example.com (kdc.crt)"), then finally
    the raw Request ID if a certificate has neither a nickname nor a
    parseable file location. Each row's own status reflects its own
    risk: `PASS`
    if healthy, `WARNING` under 30 days remaining, `CRITICAL` if
    already expired. Reports a single `INFO` row if certmonger isn't
    installed or nothing is tracked (true for every host except an
    IdM server itself, e.g. `idm1`, which typically tracks ~9
    certificates: the IPA RA agent cert, the dogtag CA/OCSP/audit/
    subsystem certs, and the server/LDAP/KDC service certs). Certmonger
    prints expiry timestamps with a local timezone abbreviation (e.g.
    `BST`) that GNU `date` can resolve ambiguously (some abbreviations
    map to more than one real timezone), so the abbreviation is
    stripped and the timestamp is parsed as the script's own local
    time instead — correct, since certmonger wrote it using that same
    host's clock. Two more checks (both NEW):
    - `CHRONY_OFFSET` — actual clock-drift magnitude in milliseconds
      from `chronyc tracking`'s "System time" line, distinct from
      Overview's binary synced/not-synced `NTP_SYNC` flag.
      `CHRONY_OFFSET_WARN_MS=1000`, `CHRONY_OFFSET_CRIT_MS=5000` —
      deliberately much tighter than Kerberos's own 5-minute default
      skew tolerance, since even sub-second drift across hosts can
      blur cross-host log correlation for the centralized-logging
      work. INFO if `chronyc` isn't installed or `chronyd` isn't
      running.
    - `KDUMP` — whether Red Hat's own kernel crash-capture service is
      active (`systemctl is-active kdump`). Pairs with `CRASH_DUMPS`
      above: kdump active + a dump present means something crashed and
      was captured for investigation; kdump inactive means a future
      crash would leave nothing behind at all. WARNING (not CRITICAL)
      if present but inactive, since some small memory-constrained VMs
      may reasonably run without a reserved `crashkernel=`. INFO if the
      `kdump` unit doesn't exist on that host.

  All of the Authentication & Identity checks degrade to an `INFO`
  row rather than failing when the relevant tool/config isn't present
  (e.g. a non-domain-joined host, or no certmonger-tracked certs) —
  they only run meaningfully on IdM/AD-joined hosts like `idm1`,
  `client1`, `nfs1` and `admin1` in this lab. None of the packages
  these checks depend on (`krb5-workstation`, `sssd-tools`,
  `certmonger`, `chrony`) are installed by this script — it only
  checks for the relevant commands with `command -v`; all ship in
  RHEL's own BaseOS/AppStream repos.

  **Deliberately out of scope for this update** (also documented in
  the script's own header comment): multipath status (see
  `RAID_STATUS` above — no SAN-attached hosts in this fleet, already a
  prior documented decision); `rpm -Va` file-integrity verification and
  slab-growth trend metrics (both need either heavy per-run cost or
  state persisted across runs that this stateless, point-in-time
  script can't provide); and application-level/HTTP/DB availability
  checks (out of scope for a host-level script — belongs to
  per-application monitoring instead). Flag any of these if you'd like
  them reconsidered.
- **System & Hardware Info** (`system_stats.sh`) — rebuilt around
  `dmidecode -s <keyword>`: BIOS (vendor/version/release date),
  baseboard (manufacturer/product), chassis type, system version/UUID,
  plus CPU topology (model, sockets, cores/socket, threads/core) from
  `lscpu`, per-DIMM memory detail (locator/size/type/speed for each
  populated slot, from `dmidecode -t 17`) and private/public IP
  addressing. System manufacturer/product-name/serial-number are
  deliberately skipped — those are already MFR/MODEL/SERIAL on the
  Overview table. Degrades gracefully (a single `DMIDECODE|WARNING` row,
  rest of the report still runs) if `dmidecode` isn't installed or the
  script isn't running as root — relevant mainly for ad hoc/manual runs,
  since this role always uses `become: true`. Also appends the full,
  unmodified output of `nmcli device show` and `ip -br a` underneath the
  table (per-device detail — hardware address, state, IPv4/IPv6
  addresses, gateway, routes, DNS — for every NIC, not just the
  primary one), shown as preformatted raw text since it doesn't fit
  the flat table shape. Degrades to a plain "not installed" line if
  either command is missing rather than failing the whole check.
- **Updates** (`dnf_updates.sh`) — read-only pending-update check; never
  installs, upgrades, or modifies anything. Detects `dnf` (falls back to
  `yum` on older hosts) and runs `check-update` to list every pending
  package (`name.arch | new-version | repo`), cross-references
  `dnf updateinfo list updates --security` on a best-effort basis to
  flag which pending packages have a security errata (package rows with
  a security fix available are reported as `WARNING`, everything else
  pending as `INFO`), and checks `needs-restarting -r` to report whether
  a reboot is already outstanding from a previously-applied update
  (`REBOOT_REQUIRED` row). A final `SUMMARY` row rolls the whole check
  up to one status: `PASS` (nothing pending, no reboot needed),
  `WARNING` (pending updates but none flagged security, or a
  non-security-only reboot outstanding), or `CRITICAL` (one or more
  pending updates carry a security errata). Degrades to a single
  `INFO` row if neither `dnf` nor `yum` is present (e.g. a non-RPM
  host slipped into the inventory), and to `WARNING` if `check-update`
  itself fails to run (network/repo problem) rather than silently
  reporting a false PASS. Exit code follows the same 3-tier convention
  as the other checks: `0` = PASS, `1` = WARNING, `2` = CRITICAL.
- **Installed Packages** (`rpm_report.sh`) - read-only inventory of
  every RPM package on the host: name, version, release, architecture
  and install date (`rpm -qa --queryformat ...`), sorted alphabetically
  by package name. This is a plain listing, not a threshold check -
  there's no PASS/WARNING/CRITICAL tier and no `STATUS` column at all,
  since "N packages installed" has no over-threshold condition to flag
  (unlike `dnf_updates.sh`'s pending-update count). Always exits `0`.
  Degrades to a single informational row if the `rpm` command itself
  isn't present, rather than failing the whole check. This is also the
  data source behind the dashboard's fleet-wide **Package Search**
  page (`/packages` - see `roles/linuxdb_dashboard/README.md`), which
  reads every host's latest `rpm_report` run to answer "which servers
  have package X installed?" without querying any host directly.

## Requirements

All six scripts rely only on standard RHEL 8/9/10 AppStream/BaseOS
tools already present on any minimal install: `df`, `findmnt`, `ps`,
`free`, `awk`, `dmidecode`, `systemctl`, `timedatectl`, `curl`,
`rpm`/`needs-restarting`, `vmstat`, `ip`, `nmcli`, `journalctl`, `dnf`
(or `yum` on older hosts) — no EPEL, no extra packages to install
(`nmcli` ships as part of NetworkManager, which RHEL installs and
enables by default; `dnf`/`yum` ship with every RHEL install by
definition).
`system_stats.sh` needs root for `dmidecode`; this role always runs
checks with `become: true`. `dnf_updates.sh` does not require root —
`check-update`, `updateinfo list` and `needs-restarting -r` are all
readable by any user — but still runs as root along with the other
scripts since the role applies `become: true` to the whole task loop.

`svr_check.sh`'s Authentication & Identity checks additionally look
for `krb5-workstation` (`kinit`/`klist`/`kdestroy`), `sssd-tools`
(`sssctl`), `certmonger` (`getcert`) and `chrony` (`chronyc`) — all
BaseOS/AppStream packages, common on IdM/AD-joined hosts. The script
only *checks for* these commands with `command -v`; it never installs
anything. On a host missing one of these tools (or not domain-joined),
the corresponding check reports `INFO` and the rest of the script
still runs normally.

The Server Health rewrite (Disk performance & storage health, Logs,
and Process & network sections above) additionally looks for `iostat`
(`sysstat` package), `lvs` (`lvm2`), `ausearch`/`getenforce` (`audit`
and `libselinux-utils`), and `ss` (`iproute`) — again all BaseOS/
AppStream, and again only *checked for*, never installed. Every one
of these checks degrades to an `INFO` row rather than failing if its
tool isn't present, so it's safe to roll this script out fleet-wide
even before confirming every host has every package — but for full
signal, worth confirming `sysstat`, `lvm2`, `audit`, `mdadm` and
`iproute` are present on the fleet's actual hosts (`admin1`, `aap1`,
`idm1`, `nfs1`, `client1`) before relying on `DISK_IO`, `LVM_THINPOOL`,
`SELINUX_AVC_6H` and `RAID_STATUS` for real coverage.

## Usage

```yaml
# group_vars/all/linuxdb_dashboard.yml
linuxdb_reports_enabled: true
```

```bash
ansible-playbook playbooks/linuxdb_dashboard.yml --tags linuxdb_reports
```

Runs against whichever hosts the play targets (see
`playbooks/linuxdb_dashboard.yml` — defaults to the `log_clients` group,
the same "every host in the linuxdb" group used by centralized logging).
Schedule this on a cron job on the control node for continuously
refreshed history — see the top-level `README.md`'s "Scheduled
reporting (cron)" section.

## Variables

- `linuxdb_reports_enabled` (bool, role default `false`) — whether this
  role's tasks run at all. Gated at the play level in
  `playbooks/linuxdb_dashboard.yml`.
- `linuxdb_reports_install_sysstat` (bool, role default `true`) —
  installs `sysstat` (BaseOS/AppStream, provides `iostat`) via the
  `package` module on every managed host before running the checks,
  since `svr_check.sh`'s `DISK_IO` check needs it and it was found
  missing fleet-wide when this check was first added. Set to `false`
  if you'd rather manage this package yourself — `DISK_IO` degrades
  gracefully to an `INFO` row when `iostat` isn't present.
- `linuxdb_reports_data_dir` (string, default
  `/linuxdb/reports`) — where collected reports are
  stored on the control node. Point this at a dedicated disk/mount if
  you have one, since history accumulates over time.
- `linuxdb_reports_remote_tmp` (string, default `/var/tmp`) — scratch
  directory on each managed host where a check script is copied to run.
- `linuxdb_reports_keep_runs` (int, default `30`) — how many timestamped
  runs to retain per host per check before older ones are pruned.
- `linuxdb_reports_checks` (list) — the check registry. Each entry is
  `{key, label, script}`; `script` must exist under
  `roles/linuxdb_reports/files/`. Add a new diagnostic script by adding
  one entry here plus dropping its script into `files/` — no task
  changes needed for a simple "run this script, save its stdout" check.

## Sources referenced

- Scripts live at `roles/linuxdb_reports/files/df_check.sh`,
  `proc_check.sh`, `svr_check.sh`, `system_stats.sh`, `dnf_updates.sh`,
  `rpm_report.sh` — this is the only copy in the repo, used both by
  this role's fleet-wide collection and by one-off ad hoc runs via
  `playbooks/push_script_and_run.yml`.
- `svr_check.sh`'s Server Health checks were grounded against:
  [Red Hat Performance Tuning Guide — iostat](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/7/html/performance_tuning_guide/sect-red_hat_enterprise_linux-performance_tuning_guide-performance_monitoring_tools-iostat)
  (disk I/O latency via `iostat`, and confirms `sysstat`'s BaseOS/
  AppStream provenance); [Red Hat Kernel Crash Dump Guide](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/7/html/kernel_administration_guide/kernel_crash_dump_guide)
  (kdump as RH's own crash-capture mechanism); `ausearch(8)`
  ([man7.org](https://man7.org/linux/man-pages/man8/ausearch.8.html))
  for the `-ts` time-window syntax used by `SELINUX_AVC_6H`; CPU
  steal-time thresholds from [ServerScout](https://www.serverscout.ie/kb/server-monitoring/cpu-steal-time-and-io-wait)
  and [ScoutAPM](https://www.scoutapm.com/blog/understanding-cpu-steal-time-when-should-you-be-worried);
  and general RHEL server-health checklist practice from
  [OneUptime's RHEL monitoring checklist](https://oneuptime.com/blog/post/2026-03-04-build-rhel-server-monitoring-checklist-production-environments/view)
  and [its RHEL 9 follow-up](https://oneuptime.com/blog/post/2026-03-04-set-up-a-comprehensive-monitoring-checklist-for-rhel-9/view).
  Red Hat itself recommends `ss` over `netstat` on modern RHEL, which
  is why `SOCKET_HEALTH` uses `ss -s` rather than parsing `netstat`
  output.
