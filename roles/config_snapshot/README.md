# Role: config_snapshot

## Purpose

Reads a configured list of whole config files off every host in the
play and saves each one back on the **control node** (the host running
`ansible-playbook`, normally `admin1`) as a timestamped snapshot per
host per file, alongside a small sha256 sidecar so a "changed since
last run" comparison never has to re-read full file content. This is
the data source `roles/linuxdb_dashboard`'s Config Files tab reads
from, so the exact contents of key config files can be reviewed —
including how they changed over time — without logging into the
server that owns them.

Modelled directly on `roles/linuxdb_reports` (same control-node
storage pattern, same timestamped-run/prune convention, same
`delegate_to: localhost` design), just capturing whole files instead
of check-script output. The file list is loosely inspired by the kind
of files a Red Hat support "sosreport"-style collect script gathers
when a case is opened, trimmed down to what's actually useful
day-to-day and reviewed for correct, existing paths across **RHEL
7/8/9/10 and Oracle Linux 7/8/9/10** — OL is a downstream rebuild of
RHEL, so the same absolute paths apply version-for-version; the
handful of items that moved or were renamed between major versions
(see `defaults/main.yml` comments) are listed explicitly rather than
branched on a per-OS `when:`, so one file list stays correct
everywhere without any host-specific logic.

Nothing is installed and nothing is written to the managed hosts
themselves — only `ansible.builtin.stat` and `ansible.builtin.slurp`
are used remotely, both read-only core modules. All state lives on the
control node.

## Behaviour

- For every path in `config_snapshot_files`, `stat`s it on the managed
  host (`become: true`, since several of these files — `/etc/shadow`-
  adjacent PAM configs, `sudoers`, `sssd.conf` — are only readable as
  root).
- A path that exists and is a regular file is read with `slurp` and
  written to the control node as:
  ```
  {{ config_snapshot_data_dir }}/<hostname>/<slug>/<timestamp>.txt      (file content)
  {{ config_snapshot_data_dir }}/<hostname>/<slug>/<timestamp>.sha256   (sha256 of that content)
  {{ config_snapshot_data_dir }}/<hostname>/<slug>/latest               (timestamp of the most recent run)
  {{ config_snapshot_data_dir }}/<hostname>/<slug>/path                 (the real absolute source path, for display)
  ```
  `<slug>` is the path with its leading `/` stripped and remaining `/`
  replaced with `__` (e.g. `/etc/ssh/sshd_config` →
  `etc__ssh__sshd_config`), so every file gets its own directory safe
  to use as-is in a URL.
- A path that doesn't exist, or exists but isn't a regular file, is
  recorded instead as `<timestamp>.missing` — this is not a failure,
  since plenty of listed paths only apply to some hosts (e.g.
  `/etc/exports` only matters on `nfs1`; `/etc/oracle-release` only
  exists on Oracle Linux). A run where a file is missing still shows
  up in the timestamp history, so "this file has never existed on this
  host" and "no data has been collected yet" stay visibly different.
- `<timestamp>` is `YYYYMMDD_HHMMSS`, generated once per play
  invocation (shared across every host and file in that run), so
  filenames sort chronologically and one run's files all share the
  same timestamp.
- After each run, older timestamped files beyond
  `config_snapshot_keep_runs` are pruned automatically, per host per
  file (`.txt`/`.sha256`/`.missing` together; `path` and `latest` are
  sidecars overwritten every run and are never pruned).
- **Denylist safety check runs first, before anything else:** every
  path in `config_snapshot_files` is checked against
  `config_snapshot_denylist_patterns` (a "contains" search, not just
  an anchored match). If any configured path matches — `/etc/shadow`,
  `/etc/gshadow`, keytabs, SSH/GPG private keys, `*.pem`, anything with
  `_vault`/`_secret(s)` in the name, etc. — the whole role refuses to
  run via `ansible.builtin.assert`, `run_once: true`,
  `delegate_to: localhost`. This is defence in depth: even if a
  credential-bearing path is added to `config_snapshot_files` by
  mistake later, it's caught before any host is ever contacted, rather
  than relying on that mistake never happening.

## Requirements

- No packages installed on the managed host — this role only reads
  files that already exist using core Ansible modules (`stat`,
  `slurp`), consistent with the hard "BaseOS/AppStream only, nothing
  ever installed" rule followed across this repo.
- `become: true` to read several of the listed files as root (e.g.
  `/etc/shadow`-adjacent PAM configs would need it if ever added —
  they deliberately aren't; `sudoers`, `sssd.conf`, and
  `/etc/audit/rules.d/audit.rules` do need it as shipped).

## Usage

```yaml
# group_vars/all/linuxdb_dashboard.yml
config_snapshot_enabled: true
```

```bash
ansible-playbook playbooks/linuxdb_dashboard.yml --tags config_snapshot
```

Runs against whichever hosts the play targets (see
`playbooks/linuxdb_dashboard.yml` — defaults to the `log_clients`
group, the same "every host in the linuxdb" group used by centralized
logging and `linuxdb_reports`). Schedule this on a cron job on the
control node for continuously refreshed history — see the top-level
`README.md`'s "Scheduled reporting (cron)" section.

## Variables

- `config_snapshot_enabled` (bool, role default `false`) — whether
  this role's tasks run at all. Gated at the play level in
  `playbooks/linuxdb_dashboard.yml`.
- `config_snapshot_data_dir` (string, default `/linuxdb/configs`) —
  where collected snapshots are stored on the control node. Sibling to
  `linuxdb_reports_data_dir`; point both at the same dedicated
  disk/mount if you have one.
- `config_snapshot_keep_runs` (int, default `60`) — how many
  timestamped runs to retain per host per file before older ones are
  pruned. Config files are typically tiny (a few KB at most), so this
  can comfortably be larger than `linuxdb_reports_keep_runs` without
  meaningfully increasing disk use.
- `config_snapshot_files` (list of absolute paths, 37 entries by
  default) — the files captured on every host in the play. Add or
  remove paths freely; nothing else in this role needs to change. A
  path that doesn't exist on a given host is recorded as "missing" for
  that run rather than causing a failure, so it's safe to list paths
  that only apply to some hosts. Covers RHEL/OL 7/8/9/10: identity &
  name resolution, networking (including the pre-NetworkManager
  `/etc/sysconfig/network` still meaningful on 7.x legacy setups),
  IdM/SSSD/Kerberos, storage/mounts/NFS, SSH/sudo/PAM/login policy,
  scheduling, time sync, firewall/SELinux/audit, logging/systemd,
  multipath/LVM, package management (both `dnf.conf` and `yum.conf`,
  since 7.x has no dnf), and OS identification
  (`os-release`/`redhat-release`/`oracle-release`).
- `config_snapshot_denylist_patterns` (list of regexes) — any
  configured path matching one of these makes the whole role refuse to
  run (see Behaviour above). Override only if you're certain a
  specific pattern is a false positive for your use case.

This role's `config_snapshot_data_dir` is also read by
`roles/linuxdb_dashboard`'s `config.py.j2`, so both roles always agree
on where snapshot data lives — see `group_vars/all/linuxdb_dashboard.yml`,
which duplicates the value for the same reason
`linuxdb_reports_data_dir` is duplicated there (a role default alone
only carries forward as hostvars for hosts that ran that role earlier
in the same invocation, and this role and the dashboard role run in
separate plays against separate host groups).

## Sources referenced

- File list scope loosely inspired by the kind of files a Red Hat
  support collect script gathers when a support case is opened — see
  [Red Hat: What is sosreport and how to create one?](https://access.redhat.com/solutions/3592).
