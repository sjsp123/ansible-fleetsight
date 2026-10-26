# ansible_operations

Ansible roles and playbooks for managing a RHEL fleet (home lab):
centralized logging, IdM health checks, host reporting, patching, and
general operational scripts. Everything here is built to run from
**RHEL's own BaseOS/AppStream repositories only** — no EPEL, no
third-party repos — so it patches cleanly via `dnf update` on any RHEL
host.

## Requirements

```bash
ansible-galaxy collection install -r requirements.yml
```

Installs `ansible.posix` (firewalld management) and
`community.postgresql` (idempotent PostgreSQL management), the two
Galaxy collections this repo depends on. Deploying chrony/NTP
(`playbooks/deploy_timesync_role.yml`, see
[`rhel_system_roles/README.md`](rhel_system_roles/README.md)) also uses
`redhat.rhel_system_roles`, but that one isn't installed by this
command - it isn't on public Galaxy at all, and ships instead as the
`rhel-system-roles` RPM from RHEL AppStream (`sudo dnf install
rhel-system-roles`).

**No internet access on the control node?** A pre-downloaded copy of
`ansible.posix` and `community.postgresql` is committed at
[`offline_collections/`](offline_collections/README.md) — install from
it instead with:

```bash
ansible-galaxy collection install -r offline_collections/requirements.yml \
  -p ./collections --offline
```

## Repository layout

```
roles/
  rsyslog_collector/    Central log collector (TCP listener, per-host files)
  rsyslog_client/       Forwards local + journald logs to the collector
  log_db/               Optional PostgreSQL log database (fast pattern search)
  log_search_ui/        Optional web search UI for the log database
  log_alerter/            Optional near-real-time email alerting on new log_db events (log_collector only)
  service_failure_watcher/ Optional centralized alerting on ANY host's systemd unit failures, via log_db (log_collector only)
  host_availability_watcher/ Optional active ping+SSH heartbeat alerting when a host stops responding entirely (log_collector only)
  disk_space_check/      Optional local disk-space + inode utilisation checking, logs breaches to syslog (every log_clients host)
  disk_space_watcher/    Optional centralized email alerting on disk_space_check threshold breaches, via log_db (log_collector only)
  chrony_check/          Optional local NTP/chrony health checking (sync, chronyd active, leap status), logs breaches to syslog (every log_clients host)
  chrony_watcher/        Optional centralized email alerting on chrony_check breaches, via log_db (log_collector only)
  idm_healthcheck_watch/  Optional local IdM health checking (runs ipa-healthcheck), logs non-SUCCESS findings to syslog (every idm host)
  idm_healthcheck_watcher/ Optional centralized email alerting on idm_healthcheck_watch findings, via log_db (log_collector only)
  service_failure_alert/ RETIRED per-host alerting via OnFailure= - disabled by default, superseded by service_failure_watcher/ (see Alerting section)
  idm_healthchecks/     Runs ipa-healthcheck on IdM masters/replicas (ad hoc/post-patch, feeds linuxdb_dashboard)
  hostinfo/             Fleet-wide host inventory/reporting
  linuxdb_reports/      Timestamped df/proc/svr_check + system_stats + dnf_updates history, per host
  config_snapshot/      Timestamped config file snapshots + change history, per host (RHEL/OL 7-10)
  linuxdb_dashboard/    Read-only web UI over hostinfo.csv + linuxdb_reports + config_snapshot history, also serves OPERATIONAL_SUPPORT_GUIDE.md at /guide (pre-rendered - see docs/render_guide.py)
  system_update/        Patch management
  push_script/          Ad-hoc script distribution and execution
  lineage/          Ad-hoc "why is this running?" process/port/file/container causality trace (no binary install needed)
  system_poweroff/      Graceful shutdown of RHEL/OL 7-10 hosts (physical HPE iLO/IPMI or virtual), with an out-of-band power-off verification check
playbooks/
  centralized_logging.yml   Rsyslog/journald + optional DB/search UI/alerter/failure-watcher/heartbeat-watcher/disk-space-watcher/chrony-watcher/idm-healthcheck-watch(er) rollout
  deploy_timesync_role.yml  Chrony/NTP deployment via redhat.rhel_system_roles.timesync - see rhel_system_roles/README.md
  service_failure_alert.yml RETIRED - per-host OnFailure= alerting, disabled by default (tears down old deployments instead - see Alerting section)
  idm_healthcheck.yml       IdM health check runs
  hostinfo.yml               Host inventory collection
  linuxdb_dashboard.yml     Diagnostic history collection + dashboard web UI
  system_health_report.yml  Fleet health/config report
  update_system.yml         Patching
  lineage.yml           Ad-hoc process/port/file/container causality trace - see roles/lineage/README.md
  system_poweroff.yml       Power down RHEL/OL 7-10 hosts and confirm they are off - see roles/system_poweroff/README.md
  collect_ilo.yml           Ad hoc HPE firmware/hardware inventory collection over ipmitool/ilorest/ssacli (no dedicated role)
  ...
group_vars/
requirements.yml
offline_collections/          Pre-downloaded Galaxy collections for offline install (see below)
rhel_system_roles/            redhat.rhel_system_roles.timesync deployment notes (see below)
STORAGE_SIZING.md             Collector-host disk sizing methodology + LVM setup
```

Each role has its own `README.md` with full variable references —
this file covers cross-cutting setup and the centralized logging rollout
specifically, since that's the newest and most involved piece.

**Sizing the collector host's disks?** See
[`STORAGE_SIZING.md`](STORAGE_SIZING.md) for how much space
`/var/log/remote`, PostgreSQL, and `/linuxdb` need at fleet scale.

**Screenshots:** [`docs/screenshots/README.md`](docs/screenshots/README.md)
has annotated screenshots of both web dashboards (`log_search_ui`,
`linuxdb_dashboard`) captured from the home lab — useful as a
reference for what each dashboard looks like.

## Environments

The repo currently drives one real environment — the home lab — whose
checkout tracks `main` directly (`git pull origin main`).

Earlier this repo also drove a second, work PoC environment from a
parallel `work` branch, with a two-branch promotion model between
them. That engagement has ended: the `work` branch and its
environment-specific files were removed (git history preserves them if
they're ever needed again).

**Environment-specific settings live in `group_vars/homelab.yml`**,
applied automatically to hosts the canonical, committed
[`inventory.ini`](inventory.ini) puts in the `[homelab]` group. It currently covers the four TLS/auth toggles
for `log_search_ui` and `linuxdb_dashboard` (on) and the lab's NTP
topology; add more variables as other settings turn out to need to
differ.

## Time synchronization: Red Hat System Roles timesync

Chrony/NTP is deployed via the upstream
`redhat.rhel_system_roles.timesync` role
(`playbooks/deploy_timesync_role.yml`) - the Red-Hat-maintained,
RHEL-native way to manage chrony, kept up to date across RHEL releases
by Red Hat. The repo's previous own-template path
(`templates/chrony.conf.j2`, `playbooks/deploy_chronyd.yml`,
`playbooks/copy_idm_chrony_conf.yml`, `ansible_deploy_chronyd.sh`) has
been removed; the system role is the only supported way to (re)deploy
chrony configuration now.

See [`rhel_system_roles/README.md`](rhel_system_roles/README.md) for
how to run it, the collection install steps (it isn't on public Galaxy
- it ships as the `rhel-system-roles` AppStream RPM), and what's
unaffected (`chrony_check` and `chrony_watcher` keep working
unchanged). NTP server lists live in `group_vars/all/site_hosts.yml`
with optional per-realm overrides in
`group_vars/<realm_group>/site_hosts.yml`; see the timesync notes'
"Per-realm NTP configuration" section before adding realm groups.
The home lab defines its own topology instead, in the tracked
`group_vars/homelab.yml`: the four clients sync from `idm1` (which
serves NTP to both lab subnets - the playbook adds the `allow`
directives), with the public pool as fallback, and `idm1` itself
tracks the pool directly.

## Centralized logging: architecture

```
 log_clients (idm1, client1, nfs1, aap1, ...)
       │  rsyslog omfwd, TCP, on-disk retry queue
       ▼
 log_collector (admin1)
       │  imtcp listener, port 6514
       ├──▶ /var/log/remote/<hostname>/<program>.log   (per-host flat files, always on)
       └──▶ PostgreSQL (log_db)  ◀── log_search_ui (pure Python stdlib WSGI, browser search)
                                      (on by default — rsyslog_collector_db_enabled etc.
                                       in group_vars/all/log_db.yml)
```

- `rsyslog_collector` + `rsyslog_client` are the baseline: every host
  forwards its logs (including `journald`, via rsyslog's built-in
  `imjournal` input) to one collector, with a retry queue so brief network
  outages don't drop messages, in addition to normal local logging.
- `log_db` + `log_search_ui` add a searchable PostgreSQL copy of the log
  stream plus a small web UI for substring/regex search by host, tag,
  and date range — useful once `grep`/`tail -f` across per-host files
  stops scaling. `log_db_enabled`, `rsyslog_collector_db_enabled`, and
  `log_search_ui_enabled` all default to `true` in
  `group_vars/all/log_db.yml`, so this layer deploys automatically
  alongside the baseline — set any of them to `false` (via `-e` or by
  editing that file) to opt a host out.

## Alerting: real-time and near-real-time

The dashboards above only surface a problem when someone opens them,
or at the next scheduled `hostinfo`/`linuxdb_reports` cron run. Several
small, independent, opt-in mechanisms close that gap by emailing you
instead of waiting to be looked at — each delivers mail through
whatever local mail transport agent is already configured on the
**collector** (no SMTP relay credentials are ever stored in Ansible or
code; see each role's README "Mail delivery"):

- **`roles/service_failure_watcher`** — centralized and fleet-wide,
  via a short systemd timer (every 30s by default) on the collector
  only, polling the `log_db` database for systemd's own universal
  `"<unit>: Failed with result '<reason>'."` message from ANY host that
  already forwards logs to the collector, then emailing one batched
  summary. No per-host mail relay needed — only the collector's own
  local MTA has to work. Part of `playbooks/centralized_logging.yml`,
  on by default via `service_failure_watcher_enabled` in
  `group_vars/all/service_failure_watcher.yml`. FLEXLM is not covered
  (it's a `pgrep` check, not a systemd unit, so it never emits that
  message) — see that role's README "What this does not cover".
- **`roles/log_alerter`** — near-real-time, via a short systemd timer
  (every 60s by default) polling the `log_db` database for new rows at
  or above a configurable severity, then emailing one batched summary.
  Part of `playbooks/centralized_logging.yml`, on by default via
  `log_alerter_enabled` in `group_vars/all/log_db.yml`. Its severity
  threshold must stay within what `rsyslog_collector_db_enabled`'s
  filter actually writes to the database — see that role's README
  "Choosing the threshold" before changing either.
- **`roles/host_availability_watcher`** — active, not log-based: a
  short systemd timer (every 60s by default) on the collector directly
  pings and TCP-checks the SSH port of every `log_clients` host, and
  emails a summary the moment a host's availability state changes
  (UP → DOWN, UP → SSH_UNREACHABLE, or back to UP). This is the only
  one of these mechanisms that can detect a host that has gone
  completely unreachable — a fully dead host cannot forward a log line
  about its own outage, which is exactly the gap the others can't
  cover. Part of `playbooks/centralized_logging.yml`, on by default via
  `host_availability_watcher_enabled` in
  `group_vars/all/host_availability_watcher.yml`.
- **`roles/disk_space_check` + `roles/disk_space_watcher`** —
  centralized and fleet-wide, mirroring `service_failure_watcher`'s own
  design: `disk_space_check` runs locally on every `log_clients` host
  (a longer, 5-minute-by-default timer, since disk/inode fill-up is
  normally slow-moving) checking real-mounted-filesystem disk-space AND
  inode percent-used via `os.statvfs()`, logging a fixed-wording
  WARNING/CRITICAL message (85%/95% thresholds, matching the dashboard's
  own `df_check.sh`) whenever a filesystem breaches; `disk_space_watcher`
  then polls `log_db` on the collector only (every 60s by default) and
  emails one batched summary the moment a new breach appears. No
  per-host mail relay needed — only the collector's own local MTA has
  to work. Part of `playbooks/centralized_logging.yml`, on by default
  via `disk_space_check_enabled`/`disk_space_watcher_enabled` in
  `group_vars/all/disk_space_check.yml`/`group_vars/all/disk_space_watcher.yml`.
- **`roles/chrony_check` + `roles/chrony_watcher`** — centralized and
  fleet-wide, mirroring `disk_space_check`/`disk_space_watcher`'s own
  design: `chrony_check` runs locally on every `log_clients` host (a
  15-minute-by-default timer, since NTP/chrony drift is normally
  slow-moving) checking NTP sync (`timedatectl`), chronyd's active
  state (`systemctl`), and chrony's leap status (`chronyc tracking`),
  logging a fixed-wording WARNING message for any abnormal result;
  `chrony_watcher` then polls `log_db` on the collector only (every
  180s by default) and emails one batched summary the moment a new
  breach appears. No per-host mail relay needed — only the collector's
  own local MTA has to work. Part of
  `playbooks/centralized_logging.yml`, on by default via
  `chrony_check_enabled`/`chrony_watcher_enabled` in
  `group_vars/all/chrony_check.yml`/`group_vars/all/chrony_watcher.yml`.
- **`roles/idm_healthcheck_watch` + `roles/idm_healthcheck_watcher`** —
  centralized and fleet-wide, mirroring `disk_space_check`/
  `disk_space_watcher`'s own design: `idm_healthcheck_watch` runs
  locally on every `idm` host (a 30-minute-by-default timer) running
  `ipa-healthcheck --output-type=json` and logging a fixed-wording
  message for every non-SUCCESS finding (or a distinct `RUN_FAILED`
  message if the check itself couldn't be run/parsed at all — unlike
  `disk_space_check`/`chrony_check`, this role's service runs as
  **root**, since `ipa-healthcheck` requires it — see that role's
  README "Why this runs as root"); `idm_healthcheck_watcher` then
  polls `log_db` on the collector only (every 60s by default) and
  emails one batched summary the moment a new finding appears. No
  per-host mail relay needed — only the collector's own local MTA has
  to work. Part of `playbooks/centralized_logging.yml`, on by default
  via `idm_healthcheck_watch_enabled`/`idm_healthcheck_watcher_enabled`
  in `group_vars/all/idm_healthcheck_watch.yml`/
  `group_vars/all/idm_healthcheck_watcher.yml`. Separate from
  `roles/idm_healthchecks` (ad hoc/post-patch runs that feed
  `roles/linuxdb_dashboard`'s IdM Health tab) — see
  `roles/idm_healthcheck_watch/README.md` for how the two coexist.

**`roles/service_failure_alert`** (the previous per-host mechanism —
a systemd `OnFailure=` drop-in wired into each covered unit on *every*
monitored host, each needing its own working local mail relay) is now
**retired and disabled by default**. `roles/service_failure_watcher`
above covers the same need fleet-wide from the collector alone, with
no per-host mail setup at all. `playbooks/service_failure_alert.yml`
still exists and now tears down any already-deployed drop-ins/script/
unit from hosts that had the old mechanism, rather than deploying it —
see that role's `group_vars/all/service_failure_alert.yml` if you ever
need to re-enable it instead.

See `roles/service_failure_watcher/README.md`,
`roles/log_alerter/README.md`, `roles/host_availability_watcher/README.md`,
`roles/disk_space_check/README.md`, `roles/disk_space_watcher/README.md`,
`roles/chrony_check/README.md`, `roles/chrony_watcher/README.md`,
`roles/idm_healthcheck_watch/README.md`,
`roles/idm_healthcheck_watcher/README.md`,
and `roles/service_failure_alert/README.md` for full variable
references and exact test procedures.

## Power management

**`roles/system_poweroff`** (via `playbooks/system_poweroff.yml`) gracefully
shuts down RHEL/OL 7-10 hosts - physical HPE ProLiant DL380 Gen10/Gen11 (via
`ipmitool`/`ilorest`, the same already-installed-CLI-tools-only pattern
`playbooks/collect_ilo.yml` uses - no new Ansible collection) or virtual -
and then confirms the power state is actually Off, not just unreachable:
out-of-band against the iLO/BMC for hosts in `[hpe_ilo_hosts]`, or a
network-reachability check for everything else.

This is deliberately hard to trigger by accident: an explicit
`-e system_poweroff_confirm=true` (there is no default confirmation) is
**always** required and never prompted for. The target host/group
(`target_hosts`) can either be passed non-interactively with
`-e target_hosts=<host_or_group>` (e.g. from AAP/cron - never prompted in
this case) or, when run by hand at a terminal without that flag, is asked
for interactively instead. Either way it refuses to target an unsupported
OS or the Ansible control node itself, and processes hosts one at a time
by default (`serial: 1`), in the order they appear in the inventory file
for that group. `--list-hosts` previews only work with the `-e` form.
See `roles/system_poweroff/README.md` for the full design, the
`ipmitool` vs. `ilorest` comparison, the BMC-credential vaulting steps,
and exactly what the VM verification path does not guarantee.

## Rollout instructions

### 1. One-time setup

```bash
git clone https://github.com/sjsp123/ansible_operations.git
cd ansible_operations
ansible-galaxy collection install -r requirements.yml
```

### 2. Inventory

Define two groups — `log_collector` (exactly one host) and `log_clients`
(every host that should forward):

```ini
[log_collector]
admin1.lab.example.com

[log_clients:children]
servers
```

The lab's real inventory is committed at [`inventory.ini`](inventory.ini)
— it is the single source of truth for both execution paths: local CLI
runs on the collector and the AAP 2.7 controller on aap27, whose
"lab-infra" inventory is sourced from this project file. The inventory
is context-free: hosts are FQDNs only, no `ansible_connection=local`
anywhere — even the collector is managed over SSH, so any control node
executes against it identically. Per-host properties live in
`host_vars/` (see `host_vars/admin1.lab.example.com.yml` for
`rsyslog_collector_advertise_host` — required by `roles/rsyslog_client`;
without it every client tries to forward logs to itself — see that
role's README for the full explanation).

If you use any of the optional site-specific host-check columns (license
manager, HPE ProLiant serials, an internal docs server, custom NTP
servers — see `group_vars/all/host_checks.yml`), copy
`group_vars/all/site_hosts.yml.example` to `group_vars/all/site_hosts.yml`
(also gitignored) and fill in your real hostnames there:

```bash
cp group_vars/all/site_hosts.yml.example group_vars/all/site_hosts.yml
```

Every one of these is optional — leave the copy unmade (or leave any
individual list out of it) and that check's column simply stays blank.

### 3. Storage: put these three paths on dedicated filesystems (do this before step 4)

**Required before your first real run against a collector host**:
`/var/log/remote` (raw flat-file logs, `roles/rsyslog_collector`),
`/var/lib/pgsql` (the PostgreSQL log database, `roles/log_db` — this is
the one that dominates disk use by far), and `/linuxdb` (report/config
snapshot/hostinfo history, `roles/linuxdb_reports` + `config_snapshot` +
`hostinfo`) must each live on their **own dedicated filesystem**, not
the root filesystem. All three grow unbounded between retention-cleanup
runs, and letting any of them share space with the OS risks a full root
filesystem taking the whole host down — keeping them independent also
means a bad size estimate on one doesn't starve the others or the OS.

Full sizing methodology, a worked lab example, and exact
`pvcreate`/`vgcreate`/`lvcreate`/`mkfs.xfs`/`fstab` commands for a
brand-new host are in [`STORAGE_SIZING.md`](STORAGE_SIZING.md)
"Recommended LVM layout." **If any of these three paths are already
populated on the root filesystem of a host you stood up before this was
a documented requirement** (the common case for an existing lab or PoC
collector), use the migration procedure in that same file's "Migrating
an already-populated host to dedicated filesystems" section instead —
it moves the existing data onto new volumes without losing it.

```bash
# Confirm where you actually stand on any given collector:
df -hT /var/log/remote /var/lib/pgsql /linuxdb
```

### 4. Full rollout (rsyslog forwarding, on by default with the log DB + search UI)

```bash
ansible-playbook playbooks/centralized_logging.yml
```

`ansible.cfg` in this repo already points at `./inventory` by default, so
no `-i` flag is needed as long as your inventory file/directory lives
there (it's gitignored — environment-specific, not committed).

This installs and configures `rsyslog_collector` + `rsyslog_client`, runs
an end-to-end forwarding verification (a uniquely tagged test message,
polled for on the collector), checks journald persistence, and — since
those variables default to `true` — ALSO deploys the PostgreSQL log
database and both search UI variants described in step 5 below. Useful
overrides:

| Override | Effect |
|---|---|
| `-e rsyslog_collector_port=<port>` | Change the TCP listener port (default 6514) |
| `-e rsyslog_forwarding_test_enabled=false` | Skip the end-to-end test message |
| `-e log_db_enabled=false -e log_search_ui_enabled=false -e rsyslog_collector_db_enabled=false` | Rsyslog forwarding only, skip the whole DB/search UI layer for this run |
| `--limit <host>` | Scope any run to specific hosts |

### 5. The log database + web search UI layer

Everything here comes from RHEL's own BaseOS/AppStream packages
(`postgresql-server`, `postgresql-contrib`, `rsyslog-pgsql`,
`python3-psycopg2`) — verified for RHEL 8, 9, and 10, no EPEL required
anywhere.

1. **Create real credentials in an Ansible Vault file** — never commit
   real passwords in plain text:
   ```bash
   ansible-vault create group_vars/all/log_db_secrets.yml
   ```
   ```yaml
   log_db_writer_password: "<a real secret>"
   log_db_reader_password: "<a different real secret>"
   ```
   `group_vars/all/*.yml` (including this vaulted file) is loaded for
   *every* host on *every* playbook run in this repo, regardless of
   which roles are enabled — so once this file exists, every playbook,
   not just `centralized_logging.yml`, needs a vault secret available.
   Set one up once instead of typing `--ask-vault-pass` on every single
   command:
   ```bash
   echo -n '<the vault password you just chose above>' > ~/.vault_pass
   chmod 600 ~/.vault_pass
   ```
   `ansible.cfg`'s `vault_password_file = ~/.vault_pass` picks this up
   automatically on every run. `--ask-vault-pass` still works fine
   alongside it if you'd rather type the password by hand sometimes.
2. **(RHEL 8 hosts only)** confirm which PostgreSQL dnf module streams
   your mirror offers before rollout:
   ```bash
   ssh <rhel8-host> sudo dnf module list postgresql
   ```
   Override the default stream if needed:
   ```yaml
   # group_vars/all/log_db.yml
   log_db_postgresql_stream: "13"   # or whichever stream your mirror carries
   ```
   RHEL 9 and RHEL 10 need no action here — PostgreSQL is non-modular on
   both, and the role's RHEL-8-only module-enable tasks are skipped
   automatically (`ansible_facts['distribution_major_version']`).
3. **Dry-run first**:
   ```bash
   ansible-playbook playbooks/centralized_logging.yml \
     --check --diff --limit <target-host>
   ```
   `log_db_enabled`, `rsyslog_collector_db_enabled`, and
   `log_search_ui_enabled` all default to `true` in
   `group_vars/all/log_db.yml`, so no `-e` flags are needed to turn this
   layer on — it deploys by default alongside the baseline rsyslog
   rollout. To opt back OUT, override explicitly:
   ```bash
   ansible-playbook playbooks/centralized_logging.yml \
     -e log_db_enabled=false -e log_search_ui_enabled=false \
     -e rsyslog_collector_db_enabled=false \
     --limit <target-host>
   ```
4. **Apply for real** (drop `--check --diff`):
   ```bash
   ansible-playbook playbooks/centralized_logging.yml --limit <target-host>
   ```
5. **Verify:**
   ```bash
   sudo systemctl status postgresql log-search-ui
   sudo -u postgres psql -d syslog -c "SELECT count(*) FROM systemevents;"
   sudo systemctl status log-db-retention-cleanup.timer
   ```
   Then open `https://<host>:8081/` in a browser (the search UI listens
   on `log_search_ui_port`, default `8081` — not port 80; TLS and HTTP
   Basic Auth are both on by default, handled in pure Python with no
   reverse proxy — see "Authentication" and "TLS" in
   `roles/log_search_ui/README.md`, and log in with the credentials from
   `group_vars/all/dashboard_auth.yml`) and run a test search. See
   `roles/log_search_ui/README.md` for its pagination controls
   ("Results per page" pulldown, an "All" option, Previous/Next links)
   and its multi-threaded request handling. Also open
   `https://<host>:8081/insights` for the heuristic data-insights page
   (auth failures, sudo activity, password failures, severity breakdown,
   top repeated alerts, top source IPs — each with clickable drill-down
   links back into Search) — see the "The Insights page" section of
   `roles/log_search_ui/README.md`.
6. **Roll out gradually** — confirm both a RHEL 8.x and a RHEL 9/10 host
   work correctly with `--limit` before widening to the rest of the
   fleet.

### 6. TLS

TLS is **on by default fleet-wide** (`rsyslog_tls_enabled: true` in
`group_vars/all/rsyslog_tls.yml`, overriding the `false` role defaults) —
server-only TLS via rsyslog's GnuTLS `gtls` driver: the collector proves
its identity to every client with a certificate, but does not request or
verify one from clients (mutual TLS was deliberately not chosen, to avoid
per-client certificate lifecycle management at fleet scale —
see `roles/rsyslog_collector/README.md` "TLS" for the full rationale). By
default `rsyslog_tls_generate_certs: true` has the collector generate its
own self-signed lab CA and certificate automatically on first run; set it
to `false` and supply your own CA/cert (e.g. from IdM/FreeIPA's
`certmonger` or an existing corporate PKI) at the `rsyslog_tls_*_path`
variables instead if your environment requires certificates from a
trusted CA. The negotiated protocol is floored at TLS 1.2 via
`rsyslog_tls_priority_string` (GnuTLS priority string, satisfies the TLS
version floor expected by NIST SP 800-52r2 and PCI DSS 4.0) — see that
variable's comments in `roles/rsyslog_collector/defaults/main.yml` for why
the GnuTLS-native syntax was used over the OpenSSL-config-style form.
See `roles/rsyslog_collector/README.md` and `roles/rsyslog_client/README.md`
for the full certificate and config details.

### 7. Dashboard authentication (local vs Kerberos/AD/IdM)

Both web dashboards (`roles/log_search_ui`, `roles/linuxdb_dashboard`)
default to a single shared local username/password (`dashboard_auth_mode:
local`, `group_vars/all/dashboard_auth.yml`), which is fine for a lab but
worth reconsidering if your organisation expects sign-in with a real
directory account rather than a shared local credential.

Both roles also support `dashboard_auth_mode: kerberos` - instead of a
locally-configured password, the same Basic Auth username/password
prompt is validated against a real Kerberos realm (FreeIPA/IdM or
Active Directory - both are just Kerberos KDCs, so the same code path
works against either) via a throwaway `kinit`, with optional group
gating. This stays pure stdlib `subprocess` plus the already-permitted
`kinit`/`kdestroy`/`getent`/`id` command-line tools - no new Python
packages, no httpd, no `mod_auth_gssapi`. See the Kerberos
authentication mode section of `roles/log_search_ui/README.md` and
`roles/linuxdb_dashboard/README.md` for the full setup and the
prerequisites to confirm (domain-join status, UPN format, and whether
the account you plan to use even allows interactive password auth)
before enabling it against a real environment.

## Pulling and redeploying updates

How to pick up a change made in this repo on a host that's already had
a playbook run against it before, such as the lab's `admin1`.

### 1. Pull the latest commits

On the control node (wherever this repo is cloned — `admin1` doubles as
both the `log_collector` host and the control node in the lab).
```bash
cd ~/ansible
git pull origin main
```

Steps 2 onward below (working out which playbook to re-run,
dry-running, applying, confirming) are identical either way.

### 2. Work out which playbook(s) to re-run

Only the playbook(s) that own the role(s) you changed need to run again
— Ansible's `copy`/`template` modules are checksum-based, so re-running a
playbook against a host that's already converged is a normal, safe,
largely-no-op operation (it only touches what actually changed).

| Role(s) changed | Playbook to re-run |
| --- | --- |
| `roles/log_search_ui`, `roles/log_db`, `roles/rsyslog_collector`, `roles/rsyslog_client`, `roles/log_alerter`, `roles/service_failure_watcher`, `roles/disk_space_check`, `roles/disk_space_watcher`, `roles/chrony_check`, `roles/chrony_watcher`, `roles/idm_healthcheck_watch`, `roles/idm_healthcheck_watcher` | `playbooks/centralized_logging.yml` |
| `roles/service_failure_alert` (RETIRED — only relevant if re-enabled) | `playbooks/service_failure_alert.yml` |
| `roles/linuxdb_reports`, `roles/config_snapshot`, `roles/linuxdb_dashboard` | `playbooks/linuxdb_dashboard.yml` |
| `roles/hostinfo` | `playbooks/hostinfo.yml` |
| `roles/idm_healthchecks` | `playbooks/idm_healthcheck.yml` |
| `roles/system_update` | `playbooks/update_system.yml` |
| `roles/system_poweroff` | `playbooks/system_poweroff.yml` |

If you're not sure which roles changed, `git log --stat` or `git diff
--stat <old-commit>..<new-commit>` against the commit you last deployed
will show every file touched.

### 3. Dry-run first

Always preview the change before applying it, scoped to the host you
care about:

```bash
ansible-playbook playbooks/centralized_logging.yml --check --diff --limit admin1
```

Review the diff output — for an app.py or config change you should see
only the file(s) you actually edited listed as changed; everything else
should report `ok` (no change).

### 4. Apply for real

```bash
ansible-playbook playbooks/centralized_logging.yml --limit admin1
```

A changed `roles/log_search_ui/files/app.py` (deployed via
`ansible.builtin.copy`, per this repo's rule that any app.py containing
literal `{ }` in embedded CSS must never go through `template`) will
show as `changed` on the copy task, which triggers the `Restart
log-search-ui` handler automatically — no manual restart needed.

**For a quick redeploy of just this role** (skipping the other roles
`centralized_logging.yml` also owns — `log_db`, `rsyslog_collector`,
`log_alerter`, etc.), scope the run with `--tags log_search_ui` instead
of the full playbook:

```bash
ansible-playbook playbooks/centralized_logging.yml --tags log_search_ui
```

`ansible_push_logsearch_change.sh` (repo root) wraps that into one
command — a `git pull` when `~/ansible` is a real git checkout, skipped
automatically otherwise — followed by the
`--tags log_search_ui` run above, the same pattern as
`ansible_pushdb_change.sh` for the LinuxDB Dashboard:

```bash
./ansible_push_logsearch_change.sh
```

If a change touched **both** apps (e.g. the cross-app navigation
switcher, which lives in both `app.py` files), use
`ansible_push_all_changes.sh` instead to redeploy both in one go — see
"Redeploying both apps at once" below.

### 5. Confirm the change actually took effect

```bash
sudo systemctl status log-search-ui
```

Check the `Active:` line's "since" timestamp is recent (i.e. the handler
restarted it just now, not hours/days ago), then check the logs are
clean:

```bash
sudo journalctl -u log-search-ui -n 20
```

No tracebacks or repeated errors should appear. Finally, reload
`https://admin1:8081/` and `https://admin1:8081/insights` in a browser
(logging in with the shared Basic Auth credentials) to sanity-check the
page still renders and the new behaviour is present.

## LinuxDB dashboard: architecture

```
 log_clients (idm1, client1, nfs1, aap1, ...)
       │  df_check.sh / proc_check.sh / svr_check.sh / system_stats.sh / dnf_updates.sh
       │  (run remotely, output pulled back — nothing left on the client)
       ▼
 log_collector (admin1)
       ├──▶ {{ linuxdb_reports_data_dir }}/<hostname>/<check>/<timestamp>.txt   (+ .rc, latest)
       │      default /linuxdb/reports — point this at a
       │      dedicated disk/mount, history accumulates over time
       ├──▶ {{ config_snapshot_data_dir }}/<hostname>/<slug>/<timestamp>.txt   (+ .sha256, .missing, path, latest)
       │      default /linuxdb/configs — curated RHEL/OL 7-10 config
       │      files (sshd_config, sssd.conf, fstab, sudoers, etc.)
       ├──▶ /linuxdb/hostinfo/hostinfo.csv   (from playbooks/hostinfo.yml, run separately)
       └──▶ linuxdb-dashboard.service (systemd, stdlib WSGI, port 8082)
              read-only browser view of all of the above, no JS, no framework
```

- `linuxdb_reports` runs the same four read-only diagnostic scripts already
  used by `playbooks/push_script_and_run.yml`, but — unlike that ad-hoc
  path, which only ever printed output live via `debug` — persists every
  run's stdout and exit code on the control node as a timestamped file,
  so there's an actual history to browse and drill into.
- `config_snapshot` reads a curated list of whole config files (37 by
  default — identity/name resolution, networking, IdM/SSSD/Kerberos,
  storage/NFS, SSH/sudo/PAM/login policy, firewall/SELinux/audit,
  logging/systemd, package management, OS identification) off every
  host, saving each one as a timestamped snapshot + sha256 sidecar on
  the control node, so any of them can be reviewed — and compared
  run-to-run — without logging into the server that owns it. Verified
  correct across RHEL and Oracle Linux 7 through 10; refuses to run if
  a credential/secret-bearing path (`/etc/shadow`, keytabs, private
  keys, etc.) is ever added to its file list by mistake.
- `linuxdb_dashboard` deploys a small read-only web front end (pure Python
  standard library — no Flask, matching `log_search_ui`'s own design)
  showing `hostinfo.csv`'s linuxdb-wide table (with a "last updated"
  timestamp and a dropdown to view any of `hostinfo`'s own retained
  history snapshots) plus, per host, tabs
  for each configured check with a history dropdown across retained runs,
  plus a Config Files tab showing `config_snapshot`'s data with a
  changed-since-last-run status badge per file. It also serves
  [`OPERATIONAL_SUPPORT_GUIDE.md`](OPERATIONAL_SUPPORT_GUIDE.md) (repo root)
  live at `/guide` — a build-time-rendered static page (see
  "Operational guide (`/guide`)" in `roles/linuxdb_dashboard/README.md`) so
  the team using the dashboard day-to-day always has the guide one click
  away, with no SSH or repo access needed.
- `linuxdb_reports_enabled`, `config_snapshot_enabled`, and
  `linuxdb_dashboard_enabled` all default to `true` in
  `group_vars/all/linuxdb_dashboard.yml` (each role's own internal
  default is `false`), so a plain `ansible-playbook playbooks/linuxdb_dashboard.yml`
  run collects fresh diagnostics, captures config files, AND (re)deploys
  the dashboard with no `-e` flags needed.

### LinuxDB dashboard rollout

1. **Run `hostinfo.yml` at least once** so there's a `hostinfo.csv` for
   the dashboard to show. It's written to `/linuxdb/hostinfo/hostinfo.csv`
   by default (`host_report_csv_path`, alongside `linuxdb_reports_data_dir`
   and `config_snapshot_data_dir` on the same `/linuxdb` mount) — confirm
   that mount exists on the control node first (`findmnt /linuxdb`):
   ```bash
   ansible-playbook playbooks/hostinfo.yml
   ```
2. **Collect diagnostic history and deploy the dashboard:**
   ```bash
   ansible-playbook playbooks/linuxdb_dashboard.yml
   ```
   Or split into steps (e.g. to deploy the UI once, then schedule
   collection separately on a timer):
   ```bash
   ansible-playbook playbooks/linuxdb_dashboard.yml --tags linuxdb_reports
   ansible-playbook playbooks/linuxdb_dashboard.yml --tags config_snapshot
   ansible-playbook playbooks/linuxdb_dashboard.yml --tags linuxdb_dashboard_ui
   ```
   **After any future change to the dashboard itself** (`app.py`,
   `config.py.j2`, the systemd unit template, etc.), you don't need the
   full playbook — just pull the repo and redeploy the UI role, which
   is exactly what `--tags linuxdb_dashboard_ui` above does: it skips
   `linuxdb_reports`/`config_snapshot` entirely, and the role's `copy`
   tasks already `notify` a handler that restarts `linuxdb-dashboard`,
   so the change is live as soon as the run finishes. `ansible_pushdb_change.sh`
   (repo root) wraps that into one command — a `git pull` when
   `~/ansible` is a real git checkout, skipped automatically
   otherwise — followed by the
   `--tags linuxdb_dashboard_ui` run above:
   ```bash
   ./ansible_pushdb_change.sh
   ```
   No `-i` flag needed in any of these — `ansible.cfg`'s
   `inventory = ./inventory` already points at the real inventory file.

   If a change touched **both** apps, use `ansible_push_all_changes.sh`
   instead to redeploy both in one go — see "Redeploying both apps at
   once" below.

   **If `OPERATIONAL_SUPPORT_GUIDE.md` itself changed** (not `app.py`),
   regenerate the page it serves before redeploying: `python3
   docs/render_guide.py` writes the updated
   `roles/linuxdb_dashboard/files/guide_content.html`, which then needs
   committing and picking up by the same `--tags linuxdb_dashboard_ui`
   redeploy above — the dashboard only ever serves whatever HTML was
   last committed, never the live `.md` file. (A `.githooks/pre-commit`
   hook guards this: with `git config core.hooksPath .githooks` set on
   your clone, every commit re-renders the guide and rejects the commit
   if the committed HTML was stale. The rendered page also carries a
   "Guide rendered <date>" footer, and the dashboard re-reads the file
   when it changes on disk, so a redeployed guide is visible without a
   service restart.)
3. **Dry-run first**:
   ```bash
   ansible-playbook playbooks/linuxdb_dashboard.yml --check --diff --limit <target-host>
   ```
4. **Verify:**
   ```bash
   sudo systemctl status linuxdb-dashboard
   journalctl -u linuxdb-dashboard
   ```
   Then open `https://<admin1>:8082/` in a browser
   (`linuxdb_dashboard_port`, default `8082`; TLS and HTTP Basic Auth
   are both on by default, handled in pure Python with no reverse proxy
   — see "Authentication" and "TLS" in `roles/linuxdb_dashboard/README.md`,
   and log in with the credentials from `group_vars/all/dashboard_auth.yml`).
5. **Schedule ongoing collection** so history and `hostinfo.csv` keep
   refreshing without a manual run each time — see
   "Scheduled reporting (cron)" below.

See `roles/linuxdb_reports/README.md`, `roles/config_snapshot/README.md`,
and `roles/linuxdb_dashboard/README.md` for full variable references,
the on-disk report/snapshot layout, and the per-check rendering rules
(table vs. raw text vs. config file viewer).

### Redeploying both apps at once

Some changes — like the cross-app navigation switcher, which lives in
both `roles/linuxdb_dashboard/files/app.py` and
`roles/log_search_ui/files/app.py` — touch both web UIs at the same
time. Rather than remembering to run both single-app scripts separately
(and risk forgetting one), run:

```bash
./ansible_push_all_changes.sh
```

This simply runs `ansible_pushdb_change.sh` followed by
`ansible_push_logsearch_change.sh`, in that order — so it always does
exactly what those two scripts do (including their own git-checkout
detection/pull and inventory handling), with nothing duplicated that
could drift out of sync with them. Both scripts already
`set -euo pipefail`, so if either playbook run fails, the combined
script stops immediately — the second app is never deployed on top of
a failed first one, matching what would happen running them by hand
one after another. If only one app changed, the matching single-app
script is still fine on its own (and slightly faster, since it only
pulls the repo once instead of twice).

## Scheduled reporting (cron)

Keeps `hostinfo.csv` and the `linuxdb_reports` diagnostic history
refreshing automatically, four times a day, with no manual run needed.
Runs from the control node (`admin1`) as the service account that
already owns this checkout and `~/.vault_pass` (e.g. `svc_ansible`).

Both scheduled jobs below write under `/linuxdb` (`hostinfo.yml` to
`/linuxdb/hostinfo/hostinfo.csv`, `linuxdb_dashboard.yml` to
`/linuxdb/reports` and `/linuxdb/configs`) — make sure that mount is
already present and writable by the service account (`findmnt
/linuxdb`) before enabling the crontab entries, since an unattended run
against a missing mount just silently falls back to creating a plain
directory on the root filesystem instead of failing loudly.

### 1. Bypass the `hostinfo.yml` interactive prompt

`playbooks/hostinfo.yml` normally asks `Enter target host or inventory
group` interactively. Always pass `-e target_host=all` (or a narrower
group/host) explicitly when running it unattended — Ansible skips
`vars_prompt` entirely whenever the variable is already supplied via
`--extra-vars`, so a cron job can never hang waiting on a prompt it has
no way to answer.

### 2. Create a log directory

```bash
mkdir -p ~/ansible/logs
```

### 3. Add the crontab entries

```bash
crontab -e
```

```cron
# Fleet hostinfo.csv + linuxdb dashboard/system health refresh
# every 6h, 4x/day: 00:00, 06:00, 12:00, 18:00
0 */6 * * * cd $HOME/ansible && /usr/bin/ansible-playbook playbooks/hostinfo.yml -e target_host=all >> $HOME/ansible/logs/cron_hostinfo.log 2>&1
5 */6 * * * cd $HOME/ansible && /usr/bin/ansible-playbook playbooks/linuxdb_dashboard.yml >> $HOME/ansible/logs/cron_dashboard.log 2>&1
```

- `cd $HOME/ansible` first is required — `ansible.cfg`
  (`inventory = ./inventory`, `vault_password_file = ~/.vault_pass`) is
  resolved relative to the current directory, and cron does not start
  in the repo by default.
- Confirm the `ansible-playbook` path matches your install
  (`which ansible-playbook`) — cron uses a minimal `PATH`, so
  hardcoding the full path avoids "command not found."
- The 5-minute stagger between the two jobs is cheap insurance against
  both playbooks hitting the same hosts simultaneously at once; not
  required for a fleet this size.

### 4. Rotate the logs

Cron output otherwise grows forever. As root, add
`/etc/logrotate.d/ansible-cron`:

```
/home/svc_ansible/ansible/logs/*.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
```

### 5. Verify

```bash
crontab -l
```

Test either line manually first, before trusting the schedule:

```bash
cd ~/ansible && ansible-playbook playbooks/hostinfo.yml -e target_host=all
```

### Why cron and not a systemd timer?

A systemd timer with `WorkingDirectory=%h/ansible` and
`User=svc_ansible` failed with `Failed at step CHDIR spawning
/usr/bin/ansible-playbook: Permission denied` — even with SELinux set
to `Permissive`, zero sandboxing directives (`ProtectHome=no` etc.,
confirmed via `systemctl show`), and `/home` confirmed to be local
disk rather than NFS (ruling out root-squash/Kerberos-ticket theories
too). The root cause was never pinned down — a PAM/session artifact
(e.g. `pam_namespace` home-directory polyinstantiation making the
interactive SSH view of `~/ansible` differ from what a bare
systemd-spawned process sees) was the leading remaining theory. Cron
runs commands via a plain shell without systemd's own
privilege-drop/CHDIR sequence, so it was unaffected and works cleanly.
If you want to revisit systemd timers later, start by comparing
`sudo runuser -u svc_ansible -- bash -c 'cd ~/ansible && pwd'` against
the interactive SSH result to see if they actually differ.

## Troubleshooting quick reference

| Symptom | Likely cause | Check |
|---|---|---|
| Clients forward but the collector never receives anything | Collector inventory address resolved to `localhost` | See §2 above; check `rsyslog_collector_advertise_host` |
| `dnf module enable postgresql:15` fails on RHEL 8 | Stream not available on your mirror/subscription | `dnf module list postgresql`, override `log_db_postgresql_stream` |
| Search UI shows no results despite logs flowing | `rsyslog_collector_db_enabled` wasn't set, or `ompgsql` isn't loaded | `psql -d syslog -c "SELECT count(*) FROM systemevents;"`; `journalctl -u rsyslog` |
| 500 error from the search page | Wrong reader DB credentials, or PostgreSQL rejecting local connections | `journalctl -u log-search-ui`; confirm `pg_hba.conf` has the reader entry |
| `log-search-ui` won't start after deploy | Syntax error in `app.py`/`wsgi_standalone_server.py`, or `python3-psycopg2` missing | `journalctl -u log-search-ui`; `python3 -m py_compile app.py` |
| PostgreSQL disk usage growing fast | Retention timer not running, or `log_db_retention_days` too high | `systemctl status log-db-retention-cleanup.timer` |
| A `log_search_ui`/`log_db` task shows `skipping` for every task, nothing deployed | The corresponding `*_enabled` variable resolved to `false` for this run (no persisted default, or a `-e ...=false` override) | Check `group_vars/all/log_db.yml`; re-run without a `false` override |
| ANY playbook — even ones unrelated to logging — fails with `Attempting to decrypt but no vault secrets found` | `group_vars/all/*.yml` (including the vaulted `log_db_secrets.yml`) loads for every host on every run; no vault secret was available | Create `~/.vault_pass` (see step 1 above) or pass `--ask-vault-pass` |
| Dashboard shows "No hostinfo.csv found" | `playbooks/hostinfo.yml` hasn't been run yet, or `linuxdb_dashboard_hostinfo_csv_path` doesn't match `host_report_csv_path` | Run `playbooks/hostinfo.yml`; confirm both paths agree (default `/linuxdb/hostinfo/hostinfo.csv`) |
| Host detail page shows the exact `ansible-playbook ... --limit <host>` command instead of data | No `linuxdb_reports` run has collected that check for that host yet | Run the suggested command, or a full `--tags linuxdb_reports` |
| `linuxdb_reports`/`config_snapshot`/`linuxdb_dashboard` tasks show `skipping` for every task | `linuxdb_reports_enabled`/`config_snapshot_enabled`/`linuxdb_dashboard_enabled` resolved to `false` for this run | Check `group_vars/all/linuxdb_dashboard.yml`; re-run without a `false` override |
| Dashboard's Config Files tab shows "No config file snapshots yet for this host" | No `config_snapshot` run has collected data for that host yet | Run the suggested command shown on the page, or a full `--tags config_snapshot` |
| `linuxdb-dashboard` service won't start | Port already in use, or `/linuxdb` mount not present on the control node | `journalctl -u linuxdb-dashboard`; `findmnt /linuxdb`; change `linuxdb_dashboard_port` |
| A cron-scheduled `hostinfo.yml` run never seems to complete/update `hostinfo.csv` | `-e target_host=all` wasn't passed, so `vars_prompt` is waiting on a prompt cron can never answer | Add `-e target_host=all` to the cron command; see "Scheduled reporting (cron)" above |
| systemd timer's service fails with `Failed at step CHDIR ... Permission denied`, even though the same path works fine over SSH | Not yet root-caused in this lab (SELinux, sandboxing, and NFS were all ruled out) | Use cron instead (see "Scheduled reporting (cron)" above); if investigating further, compare `sudo runuser -u <user> -- bash -c 'cd <dir> && pwd'` against the interactive SSH result |

## Sources referenced

- [Red Hat: Remote Syslogging with rsyslog on Red Hat Enterprise Linux](https://access.redhat.com/articles/3549872)
- [Red Hat: Installing RHEL 8 content (AppStream module streams, incl. postgresql)](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/8/html/installing_managing_and_removing_user-space_components/installing-rhel-8-content_using-appstream)
- [Red Hat Developer: Discover packaging parallel database streams in RHEL 10](https://developers.redhat.com/articles/2025/03/11/discover-packaging-parallel-database-streams-rhel-10)
- [rsyslog: Encrypting Syslog Traffic with TLS (SSL)](https://docs.rsyslog.com/doc/tutorials/tls.html)
- [FreeIPA: Certmonger and ipa-getcert usage](https://www.freeipa.org/page/Certmonger)
