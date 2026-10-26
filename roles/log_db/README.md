# Role: log_db

## Purpose

Sets up a local PostgreSQL database that stores centrally-collected log
messages, so they can be searched by pattern instead of grepping flat
files across the fleet. Written to by `roles/rsyslog_collector` (when
`rsyslog_collector_db_enabled` is true) and read by `roles/log_search_ui`.
Intended to run on the same host as `roles/rsyslog_collector` (the
`log_collector` inventory group). See `playbooks/centralized_logging.yml`.

Deliberately built entirely from RHEL AppStream packages
(`postgresql-server`, `postgresql-contrib`) plus the `community.postgresql`
Ansible collection for idempotent database management, so the whole stack
patches via `dnf update` - no third-party repos, containers, or
compiled-from-source components. Tested against RHEL 8, 9, and 10 - see
"RHEL 8/9/10 differences" below, since RHEL 8 packages PostgreSQL
differently from 9 and 10.

## Behaviour

- On RHEL 8 only: checks which `postgresql` dnf module stream is enabled
  and, if it isn't already `log_db_postgresql_stream`, enables that stream
  before installing anything (idempotent - skipped entirely on RHEL 9/10).
  See "RHEL 8/9/10 differences" below.
- Installs `postgresql-server`, `postgresql-contrib` (provides the
  `pg_trgm` extension used for fast pattern search), and `python3-psycopg2`
  (needed by both this role and `roles/log_search_ui`).
- Initializes the database cluster (`postgresql-setup --initdb`) if not
  already done.
- Sets `listen_addresses` in `postgresql.conf` (default `localhost` only -
  no network exposure unless you deliberately change it).
- Adds password-authenticated `pg_hba.conf` entries scoped narrowly to the
  writer and reader roles below, connecting from localhost only.
- Creates the `log_db_name` database (default `syslog`) and enables the
  `pg_trgm` extension in it.
- Creates `log_db_table` (default `systemevents`) with columns matching
  what `roles/rsyslog_collector`'s database output writes: `message`,
  `facility`, `fromhost`, `priority`, `devicereportedtime`, `receivedat`,
  `syslogtag`, plus a `pg_trgm` GIN index on `message` (for fast
  substring/pattern search) and B-tree indexes on `fromhost`, `receivedat`,
  and `syslogtag`.
- Creates two least-privilege database roles:
  - `log_db_writer_user` (default `rsyslog`) - **INSERT-only** on the log
    table. Used by `roles/rsyslog_collector`'s database output.
  - `log_db_reader_user` (default `log_search`) - **SELECT-only** on the
    log table. Used by `roles/log_search_ui`.
- Creates `log_db_suppressed_patterns_table` (default
  `log_search_ui_suppressed_patterns`) - a small table backing
  `roles/log_search_ui`'s Insights "Suppress/dismiss known-noisy
  patterns" feature (see that role's README). Columns: `id` (BIGSERIAL
  primary key), `service`, `pattern`, `note`, `suppressed_by`,
  `suppressed_at` (defaults to `now()`), with a `UNIQUE (service,
  pattern)` constraint so re-dismissing an already-suppressed pattern
  updates the existing row instead of erroring or duplicating.
  `log_db_reader_user` is granted **INSERT/SELECT/UPDATE/DELETE scoped to
  this one table only** (plus `USAGE`/`SELECT` on its id sequence) - the
  main log table's SELECT-only grant is unchanged. This table holds no
  log content, only the reviewer's own service/pattern/note choices, so
  widening the reader role's privileges here (and only here) doesn't
  weaken the SELECT-only guarantee on actual log data. UPDATE is
  required (not just INSERT/SELECT/DELETE) because the app's
  `suppress_pattern()` uses `INSERT ... ON CONFLICT (service, pattern)
  DO UPDATE SET ...` - Postgres checks UPDATE privilege on the table for
  the whole statement whenever a `DO UPDATE` clause is present, even on
  rows that insert cleanly with no actual conflict.
- Installs a daily systemd timer (`log-db-retention-cleanup.timer`) that
  deletes rows older than `log_db_retention_days` and runs `VACUUM`.

## Variables

### defaults/main.yml

- `log_db_postgresql_stream` (string, default `"15"`) — **RHEL 8 only**.
  Which `postgresql` dnf module stream to enable before install. Ignored
  on RHEL 9/10. See "RHEL 8/9/10 differences" below.
- `log_db_name` (string, default `syslog`) — database name.
- `log_db_table` (string, default `systemevents`) — log messages table.
- `log_db_writer_user` / `log_db_writer_password` (default `rsyslog` /
  `CHANGE_ME_rsyslog_writer`) — INSERT-only role used by the collector.
- `log_db_reader_user` / `log_db_reader_password` (default `log_search` /
  `CHANGE_ME_log_search_reader`) — SELECT-only role used by the search UI.
- `log_db_retention_days` (int, default `90`) — row retention window.
- `log_db_retention_cleanup_enabled` (bool, default `true`) — whether the
  daily cleanup timer is installed.
- `log_db_listen_addresses` (string, default `localhost`) — passed
  straight into `postgresql.conf`'s `listen_addresses`.
- `log_db_pg_hba_extra` (list of strings) — `pg_hba.conf` lines appended
  as a managed block. Defaults to password-auth entries for the writer and
  reader roles from localhost only.
- `log_db_suppressed_patterns_table` (string, default
  `log_search_ui_suppressed_patterns`) — table backing the Insights
  "Suppress/dismiss known-noisy patterns" feature in `roles/log_search_ui`
  (see "Behaviour" above). `roles/log_search_ui`'s own
  `log_search_ui_suppressed_patterns_table` defaults to this same value,
  so overriding it here keeps both roles in sync automatically.

## RHEL 8/9/10 differences

All three major versions carry the packages this role needs
(`postgresql-server`, `postgresql-contrib`, `python3-psycopg2`) in
AppStream/BaseOS - no EPEL, ever - but RHEL 8 packages PostgreSQL
differently from 9 and 10:

- **RHEL 8** still uses the legacy dnf module system: PostgreSQL ships as
  a `postgresql` module with several parallel streams (`9.6`, `10`, `12`,
  `13`, `15` on RHEL 8.10). The unversioned `postgresql-server` /
  `postgresql-contrib` RPM names stay the same regardless of which
  stream is enabled, but **without any action you get stream `10`**
  (released 2017 - old, though still Red Hat-supported for the RHEL 8
  life cycle). This role checks `dnf module list --enabled postgresql`
  and, if `log_db_postgresql_stream` (default `"15"`) isn't already
  enabled, runs `dnf module enable postgresql:<stream>` before
  installing. Run `dnf module list postgresql` on your own RHEL 8.10 host
  first to confirm which streams your mirror actually carries, and
  override `log_db_postgresql_stream` in `group_vars/all/log_db.yml` if
  you want a different one. Switching an *already-installed* stream
  later requires manually running `dnf module reset postgresql` first
  (Red Hat's own docs) - this role does not attempt that automatically,
  since it can require removing the currently-installed packages first.
- **RHEL 9** dropped that requirement for the initial version: a plain
  `dnf install postgresql-server` installs PostgreSQL 13 directly with
  no module step (optional newer streams like 15/16/18 are available via
  `dnf module install postgresql:16/server` if you want them, but this
  role doesn't need that - PostgreSQL 13 is more than enough for log
  storage/search). The RHEL-8-only tasks above are skipped entirely.
- **RHEL 10** (including 10.2) went further and dropped the module
  system for PostgreSQL altogether: `dnf install postgresql-server`
  installs PostgreSQL 16 directly as a normal non-modular AppStream
  package (Red Hat's "parallel database streams" packaging) - also no
  module step, same as RHEL 9 in practice for this role's purposes. The
  RHEL-8-only tasks are skipped here too.

In short: this role runs unmodified on RHEL 8, 9, or 10 - the only
moving part across versions is which PostgreSQL release you end up with,
and that has no effect on the schema, indexes, or roles this role
creates.

## Securing credentials

**The shipped default passwords are placeholders — change them before
using this outside a throwaway lab.** The recommended approach, matching
this repo's existing conventions:

```bash
ansible-vault create group_vars/all/log_db_secrets.yml
```

containing:

```yaml
log_db_writer_password: "<a real secret>"
log_db_reader_password: "<a different real secret>"
```

Then run playbooks with `--ask-vault-pass` (or a vault password file).
Never commit real passwords to `group_vars/all/log_db.yml` in plain text.

## Requirements

- `community.postgresql` collection:
  ```bash
  ansible-galaxy collection install -r requirements.yml
  ```

## Usage

```yaml
- name: Configure the central log database
  hosts: log_collector
  become: true
  roles:
    - role: log_db
```

## Scaling notes for ~160 servers (work rollout)

- The default schema/indexes comfortably handle a single fleet's syslog
  volume on modest hardware, but at that scale watch disk growth closely -
  size the storage volume against actual daily log volume, not the
  home-lab's 4 hosts (see the top-level write-up's LVM sizing section).
- If ingest volume becomes the bottleneck, consider partitioning
  `log_db_table` by month/week (PostgreSQL native table partitioning) so
  the retention cleanup job can `DROP` old partitions instantly instead of
  `DELETE`-ing rows.
- If you outgrow a single collector+DB host, splitting the database onto
  its own host is supported by this design (set `log_db_listen_addresses`
  and `log_db_pg_hba_extra` to allow the collector's real address, and
  point `rsyslog_collector_db_host` / `log_search_ui_db_host` at it) -
  just note that widens the network exposure beyond the current
  localhost-only default, so restrict `pg_hba.conf` and firewalld
  accordingly.
