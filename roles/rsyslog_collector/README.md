# Role: rsyslog_collector

## Purpose

Configures a host as the central log collector for the fleet. Opens a TCP
syslog listener and stores each remote host's messages in its own file
under `rsyslog_remote_log_dir`, split by originating program, with daily
log rotation. Intended to be paired with `roles/rsyslog_client` on every
other host. See `playbooks/centralized_logging.yml`.

## Behaviour

- Ensures `rsyslog` is installed and enabled.
- Creates `rsyslog_remote_log_dir` (default `/var/log/remote`).
- Deploys `/etc/rsyslog.d/10-remote-collector.conf` with an `imtcp` input
  on `rsyslog_collector_port` and a per-source-host dynamic file template:
  `<rsyslog_remote_log_dir>/%HOSTNAME%/%PROGRAMNAME%.log`.
- Opens `rsyslog_collector_port/tcp` in `firewalld` if firewalld is
  running (requires the `ansible.posix` collection).
- Sets up `/etc/logrotate.d/remote-logs` (daily, `rsyslog_remote_log_retain_days`
  days retained, compressed, HUPs rsyslog on rotate).
- Locally-generated messages on the collector itself are unaffected and
  continue to `/var/log/messages` etc. as normal — only messages received
  from other hosts (`$fromhost-ip != 127.0.0.1`) are written to the
  per-host store.
- When `rsyslog_collector_db_enabled` is `true`: also installs
  `rsyslog-pgsql` and deploys `/etc/rsyslog.d/05-remote-collector-db.conf`,
  which writes every received remote message into the PostgreSQL database
  set up by `roles/log_db`, in addition to (not instead of) the flat
  files above. See `roles/log_db/README.md` and `roles/log_search_ui/README.md`.
  Numbered `05-` (loaded before `10-remote-collector.conf`) because that
  file's flat-file rule ends with `stop`, which would otherwise discard
  every remote message before a later-loaded file's action ever saw it.

## Database log filtering

By default only a curated, audit-focused subset of remote messages is
written into the PostgreSQL database that `log_search_ui` / `linuxdb_dashboard`
query — not the full firehose. **The flat files under `rsyslog_remote_log_dir`
are unaffected by this filter and continue to capture every message at every
severity as a full forensic backup**; only what becomes searchable in the
dashboards is trimmed. This was added because a full unfiltered feed is
dominated by routine debug/info chatter that buries the events an auditor
or on-call engineer actually needs to find, a problem several SIEM/logging
guides call out directly:

- Red Hat's own official remote-logging example config
  ([Security hardening: configuring a remote logging solution](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/security_hardening/assembly_configuring-a-remote-logging-solution_security-hardening))
  keeps `authpriv` (auth/sudo/su/PAM) at every severity in its own bucket,
  and buckets everything else at `info` and above (excluding only `debug`).
  Filtering happens at the receiving/collector server, not the client —
  the pattern this role follows.
- Wazuh's log management guidance
  ([codesecure.in](https://codesecure.in/blogs/wazuh-log-management-strategy-what-to-collect)):
  "Not every log deserves a place in the indexer... verbose debug logging,
  successful health-check probes, repetitive load-balancer pings and chatty
  application info messages... dominate ingestion while contributing almost
  nothing to detection," and recommends separating what you *store* from
  what you *alert on* — exactly the flat-file-vs-database split this role
  already has.
- Bindplane/Elastic ([bindplane.com](https://bindplane.com/blueprints/optimize-syslog-for-elasticsearch))
  and Last9 ([last9.io](https://last9.io/blog/what-are-syslog-levels/))
  both recommend dropping `debug` (and often `info`) from general search/alerting
  indices while explicitly calling out that `notice`/`info` still has audit
  value and should be retained for reporting/compliance purposes even if
  excluded from day-to-day dashboards — hence the always-keep facility list
  below rather than a blanket severity cutoff.
- PCI DSS Requirement 10.2 ([lorikeetsecurity.com](https://lorikeetsecurity.com/blog/pci-dss-logging-monitoring-requirement-10))
  mandates capturing all privileged/admin actions and authentication-
  mechanism changes regardless of severity level — the reason `authpriv`
  (and `cron`, for scheduled-job auditing) bypass the severity filter
  entirely rather than being folded into the general bucket.

### How it works

`05-remote-collector-db.conf.j2` only forwards a remote message into
`ompgsql` if, in addition to the existing `$fromhost-ip != '127.0.0.1'`
check:

- its facility is in `rsyslog_collector_db_always_facilities` (default
  `authpriv`, `cron`) — kept at **every** severity including debug; or
- its severity is `rsyslog_collector_db_min_severity` (default `5` =
  `notice`) **or more urgent** (lower RFC 5424 number = more urgent:
  0=emerg ... 4=warning, 5=notice, 6=info, 7=debug).

So by default: `notice`/`warning`/`err`/`crit`/`alert`/`emerg` from any
facility, plus every severity (including `info`/`debug`) from `authpriv`
and `cron`, reach the database. Routine `info`/`debug` chatter from every
other facility (e.g. `local0`-`local7`, `daemon`, `user`) is dropped from
the database only — it's still in the flat files.

### Before rolling this out: verify the always-keep facility list against your own data

`authpriv` is the correct facility for sudo/su/sshd/PAM session events on
stock RHEL (the RHEL sudo package is built with `--with-logfac=authpriv`),
but a custom `Defaults syslog=...` line in `/etc/sudoers`, or a non-default
PAM configuration, can redirect this to a different facility in your
environment. Since the database already has the full unfiltered firehose
captured before you change these defaults, check what's actually being
stored for your sudo/su events first:

```sql
SELECT facility, priority, syslogtag, count(*)
FROM systemevents
WHERE syslogtag ILIKE 'sudo%' OR syslogtag ILIKE 'su:%'
GROUP BY facility, priority, syslogtag
ORDER BY count(*) DESC
LIMIT 20;
```

(`facility`/`priority` are stored as their RFC 5424 numeric codes, not
names — facility `10` = `authpriv`, `9` = `cron`; the standard mapping is
`facility = code`, e.g. `0`=kern, `1`=user, `2`=mail, `3`=daemon, `4`=auth,
`5`=syslog, `6`=lpr, `7`=news, `8`=uucp, `9`=cron, `10`=authpriv,
`11`=ftp, `16`-`23`=`local0`-`local7`.) If sudo/su rows show a facility
other than `10` (`authpriv`), add that facility's name (e.g. `local2`) to
`rsyslog_collector_db_always_facilities` before enabling the filter.

### Tuning

`rsyslog_collector_db_min_severity` and `rsyslog_collector_db_always_facilities`
are both plain role defaults — override per-environment via `group_vars`/
`-e` the same way as any other variable in this role. Set
`rsyslog_collector_db_min_severity: 7` to effectively disable the severity
half of the filter (store everything, same as before this change); raise it
toward `4` (warning) for a tighter dashboard if `notice` still feels noisy.

## TLS

Set `rsyslog_tls_enabled: true` (must also be set on `roles/rsyslog_client`)
to encrypt the client -> collector transport using rsyslog's `gtls`
(GnuTLS) `StreamDriver`, package `rsyslog-gnutls` (BaseOS/AppStream, no
EPEL). This is **server-only TLS**, not mutual TLS: the collector proves
its identity to every client via a certificate signed by a self-signed
lab CA, and the whole transport is encrypted, but the collector does not
request or check a certificate from clients. That keeps per-client
certificate issuance/renewal out of the picture, which matters once this
role is managing more than a handful of hosts.

On first run with `rsyslog_tls_enabled: true`, this role (via plain
`openssl` commands, the same idiom used by `roles/linuxdb_dashboard` and
`roles/log_search_ui` for their own self-signed certs — no
`community.crypto` collection needed):

1. Generates a self-signed CA key + certificate at `rsyslog_tls_ca_key_path`
   / `rsyslog_tls_ca_cert_path` (default `/etc/pki/rsyslog/rsyslog-ca-*.pem`),
   if not already present.
2. Generates the collector's own key + CSR, with a `subjectAltName` of
   `rsyslog_tls_cn` (defaults to `rsyslog_collector_advertise_host`, then
   `ansible_fqdn`, then the inventory hostname).
3. Signs that CSR with the lab CA to produce the collector's certificate
   at `rsyslog_tls_cert_path`.
4. Reads the CA certificate's content and exposes it as a runtime fact
   (`rsyslog_tls_ca_cert_content`) so `roles/rsyslog_client` can read it
   via `hostvars` **within the same playbook run** and trust it — no CA
   private key, and no collector private key, is ever copied off this
   host.
5. Re-registers `rsyslog_tls_cn` as a runtime fact (same value, just
   promoted to fact precedence) so it's also reliably readable via
   `hostvars` from `roles/rsyslog_client`. It starts life as a plain role
   default, which is not dependable via `hostvars` from a different
   host/play — without this, `rsyslog_tls_permitted_peer` on the client
   side resolves empty and the client's fail-fast assertion trips.

Every generation step is guarded by Ansible's `creates:`, so once the CA
and collector cert exist they are left alone on every later run — exactly
like `roles/linuxdb_dashboard`'s self-signed cert. If you'd rather use
certificates from an existing CA (e.g. your IdM CA) instead of a fresh
lab-only one, set `rsyslog_tls_generate_certs: false` and place your own
files at the `rsyslog_tls_ca_cert_path` / `rsyslog_tls_ca_key_path` /
`rsyslog_tls_cert_path` / `rsyslog_tls_key_path` paths before running this
role — it will use them as-is and never overwrite them.

Toggling `rsyslog_tls_enabled` back to `false` leaves all generated
cert/key files in place untouched; it just switches the deployed rsyslog
config back to the plain `imtcp` listener.

The `imtcp` listener's negotiated protocol is floored at TLS 1.2 via
`rsyslog_tls_priority_string` (a GnuTLS priority string passed to
`module(load="imtcp" ... gnutlsPriorityString="...")`), refusing TLS
1.0/1.1/SSLv2/SSLv3 handshakes and preferring an AEAD/forward-secrecy
cipher suite — satisfies the TLS version floor expected by NIST SP
800-52r2 and PCI DSS 4.0. Deliberately uses GnuTLS-native colon-separated
priority-string syntax rather than the OpenSSL-config-style
`Protocol=ALL,...` form, since the latter is known to throw a parse error
on some RHEL 8 rsyslog builds ([Red Hat KB](https://access.redhat.com/solutions/6172242)).
Must match `roles/rsyslog_client`'s copy of this same variable.

## Variables

### defaults/main.yml

- `rsyslog_collector_port` (int, default `6514`)
  TCP port the collector listens on. 6514 is used as a common convention
  for "syslog over a non-default port" to avoid clashing with an existing
  UDP/514 setup — change it if you'd rather use something else.

- `rsyslog_remote_log_dir` (string, default `/var/log/remote`)
  Base directory for received per-host logs.

- `rsyslog_remote_log_retain_days` (int, default `14`)
  Logrotate retention for received remote logs.

- `rsyslog_collector_db_enabled` (bool, role default `false`, persisted
  to `true` in this repo's `group_vars/all/log_db.yml`)
  Whether to also write incoming messages into the PostgreSQL database
  from `roles/log_db`, alongside the existing flat files. Run `log_db` on
  this host first.

- `rsyslog_collector_db_host` / `rsyslog_collector_db_name` /
  `rsyslog_collector_db_table` / `rsyslog_collector_db_user` /
  `rsyslog_collector_db_password`
  Connection details for the database output. Default to the matching
  `log_db_*` variables so both roles stay in sync automatically as long as
  you only override the `log_db_*` versions.

- `rsyslog_collector_db_min_severity` (int, default `5` = `notice`)
  RFC 5424 severity threshold for what reaches the database (lower =
  more urgent; messages at this value or more urgent are kept). Does not
  affect the flat files. See "Database log filtering" above.

- `rsyslog_collector_db_always_facilities` (list of strings, default
  `[authpriv, cron]`)
  Facilities exempted from the severity filter above and always written
  to the database at every severity. See "Database log filtering" above.

- `rsyslog_tls_enabled` (bool, default `false`)
  Turns on server-only TLS for the collector's listener. Must match
  `rsyslog_tls_enabled` in `roles/rsyslog_client`. See "TLS" above.

- `rsyslog_tls_dir` (string, default `/etc/pki/rsyslog`)
  Directory holding all CA/cert/key material on the collector.

- `rsyslog_tls_ca_cert_path` / `rsyslog_tls_ca_key_path` / `rsyslog_tls_cert_path`
  / `rsyslog_tls_key_path`
  Paths for the lab CA and the collector's own certificate/key, under
  `rsyslog_tls_dir` by default.

- `rsyslog_tls_generate_certs` (bool, default `true`)
  Whether this role generates the lab CA and collector cert itself. Set
  `false` to supply your own files (e.g. from an existing IdM CA) instead.

- `rsyslog_tls_ca_days` (int, default `3650`) / `rsyslog_tls_cert_days`
  (int, default `825`)
  Validity periods for the generated CA and collector certificate.

- `rsyslog_tls_cn` (string, default `rsyslog_collector_advertise_host` ->
  `ansible_fqdn` -> inventory hostname)
  Name (CN + subjectAltName) on the generated collector certificate.
  Clients validate against this exact name.

- `rsyslog_tls_priority_string` (string, default
  `"NORMAL:-VERS-TLS1.0:-VERS-TLS1.1"`)
  GnuTLS priority string enforced on the `imtcp` listener, floor-
  restricting the negotiated protocol to TLS 1.2+. Must match
  `roles/rsyslog_client`'s copy of this same variable. See "TLS" above.

## Requirements

- `ansible.posix` collection (for the `firewalld` module). When
  `rsyslog_collector_db_enabled` is used, `roles/log_db` must also have
  run on this host first.
  ```bash
  ansible-galaxy collection install -r requirements.yml
  ```

## Usage

```yaml
- name: Configure central log collector
  hosts: log_collector
  become: true
  roles:
    - role: rsyslog_collector
```

## End-to-end forwarding verification

`playbooks/centralized_logging.yml` includes a third play, run after both
roles, that checks (with retries) whether each client's uniquely-tagged
test message (sent by `roles/rsyslog_client`) actually landed under
`rsyslog_remote_log_dir` on this collector — proof forwarding works end to
end, not just that config was deployed. It fails the playbook run and
names the affected hosts if any client's message doesn't arrive within the
retry window. Skip it with `-e rsyslog_forwarding_test_enabled=false`.

## Notes / next steps

- **Security**: TLS is available (see "TLS" above); off by default in
  this role's own `defaults/main.yml`, but turned on fleet-wide by this
  repo's `group_vars/all/rsyslog_tls.yml`. With it enabled, the
  transport is encrypted and clients verify the collector's identity;
  it is not mutual TLS, so a host that can reach
  the collector's port can still open a TCP connection and attempt the
  TLS handshake without a client certificate of its own (the collector
  doesn't ask for one). If you need clients to be individually
  authenticated too, mutual TLS (a certificate per client) is the next
  step up, at the cost of a per-client certificate lifecycle.
- Exactly one host should be in the `log_collector` inventory group —
  `roles/rsyslog_client` asserts this group exists and is non-empty.
