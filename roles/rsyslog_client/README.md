# Role: rsyslog_client

## Purpose

Configures a host to forward its logs — including `journald`, via
rsyslog's built-in `imjournal` input on RHEL, so no separate journald
forwarding daemon is needed — to the central collector configured by
`roles/rsyslog_collector`, while keeping full local logging intact. See
`playbooks/centralized_logging.yml`.

## Behaviour

- Fails fast if the `log_collector_group` inventory group is missing or
  empty, so a misconfigured inventory is caught before any changes.
- Resolves the collector's address from that group's first (and only)
  host, preferring `rsyslog_collector_advertise_host` if set on that host,
  then `ansible_host`, then falling back to its inventory hostname.
- Fails fast if that resolved address is `localhost`/`127.0.0.1` (which
  every client would otherwise interpret as itself) — this catches the
  common case where the collector is entered in inventory as `localhost`
  (e.g. with `ansible_connection=local`) without a real reachable address
  set.
- Ensures `rsyslog` is installed and enabled.
- Sets `global(localHostName=...)` to `rsyslog_client_local_hostname`
  (the fully qualified `inventory_hostname` by default) so every message
  this host emits — local and forwarded alike — reports that name as its
  HOSTNAME field, regardless of what this box's own hostname happens to
  be set to. This matters because the collector's DB insert template
  stores the HOSTNAME field as the `fromhost` column, and every watcher
  app (`disk_space_watcher`, `chrony_watcher`, `service_failure_watcher`,
  `host_availability_watcher`, `log_alerter`) shows that column's value
  directly in alert emails — so this is what makes those emails show
  fully qualified hostnames end to end. Only affects new messages sent
  after this role next runs.
- Deploys `/etc/rsyslog.d/90-remote-forwarding.conf`, forwarding every
  message to the collector over TCP via `omfwd`, with an on-disk retry
  queue (`queue.type=linkedList`, up to 1 GB, `resumeRetryCount=-1`) so a
  network blip doesn't drop messages. Local logging is not stopped —
  `/var/log/messages` etc. continue as normal.
- Optionally switches `journald` to persistent storage by dropping
  `/etc/systemd/journald.conf.d/00-persistent-storage.conf` with
  `Storage=persistent`, so the journal itself survives reboots. A drop-in
  override is used (rather than editing `/etc/systemd/journald.conf`
  directly) so it works even on minimal images where that base file isn't
  present, and doesn't conflict with any vendor-managed copy of it.
- Verifies the change took effect: checks `/var/log/journal` exists and
  reports (via a debug message) how many boots' worth of history
  `journalctl --list-boots` currently shows. If it already spans more than
  one boot, persistence is already proven. If not (e.g. straight after
  first enabling it), the message tells you persistence isn't provable yet
  — it will confirm itself naturally the next time the host reboots for
  any other reason (patching, maintenance, etc.).
- Sends a uniquely-tagged test log message (`logger -t rsyslog_forwarding_test
  "rsyslog_forwarding_test_token=..."`) right after the forwarding config
  goes live, so the playbook's separate collector-side verification play can
  confirm it actually arrived — proof that forwarding works end to end, not
  just that config was deployed. Set `rsyslog_forwarding_test_enabled: false`
  to skip this.

## Variables

### defaults/main.yml

- `log_collector_group` (string, default `"log_collector"`)
  Inventory group expected to contain exactly one collector host.

- `rsyslog_collector_advertise_host` (string, no default — set per-host in
  inventory on the collector host, not here)
  The address other hosts should use to reach the collector. Required
  whenever the collector's inventory hostname/`ansible_host` isn't
  reachable from other hosts — most commonly when the collector is
  entered as `localhost` with `ansible_connection=local` so the controller
  manages it directly. Example, in inventory:
  ```ini
  [log_collector]
  localhost ansible_connection=local rsyslog_collector_advertise_host=admin1.lab.local
  ```

- `rsyslog_collector_port` (int, default `6514`)
  Must match `rsyslog_collector_port` used by `roles/rsyslog_collector`.

- `journald_persistent_storage` (bool, default `true`)
  Whether to switch journald to persistent on-disk storage.

- `rsyslog_forwarding_test_enabled` (bool, default `true`)
  Whether to send the uniquely-tagged end-to-end forwarding test message.

- `rsyslog_forwarding_test_tag` (string, default `"rsyslog_forwarding_test"`)
  Syslog tag used for the test message. Must match the value used in the
  "Verify end-to-end log forwarding from clients" play in
  `playbooks/centralized_logging.yml`.

- `rsyslog_tls_enabled` (bool, default `false`)
  Must match `rsyslog_tls_enabled` in `roles/rsyslog_collector`. Encrypts
  forwarding and verifies the collector's identity against its lab CA.
  See "TLS" below.

- `rsyslog_tls_dir` (string, default `/etc/pki/rsyslog`) /
  `rsyslog_tls_ca_cert_path` (string, default
  `{{ rsyslog_tls_dir }}/rsyslog-ca-cert.pem`)
  Where this client stores the collector's CA certificate once fetched.

- `rsyslog_tls_permitted_peer` (string, default: the collector's own
  `rsyslog_tls_cn`, read via `hostvars`)
  The exact name this client requires on the collector's certificate.
  Only override if a client needs to expect a different name than the
  collector generated for itself (uncommon).

- `rsyslog_tls_priority_string` (string, default
  `"NORMAL:-VERS-TLS1.0:-VERS-TLS1.1"`)
  GnuTLS priority string passed to the `omfwd` forwarding action's
  `gnutlsPriorityString` parameter, floor-restricting the negotiated
  protocol to TLS 1.2+. Must match `roles/rsyslog_collector`'s copy of
  this same variable — see that role's README "TLS" section for why the
  GnuTLS-native syntax was chosen over the OpenSSL-config-style form.

- `rsyslog_client_local_hostname` (string, default `"{{ inventory_hostname }}"`)
  What this client reports as its HOSTNAME field in every message —
  ultimately what every watcher alert email shows as the affected host.
  Defaults to `inventory_hostname` rather than `ansible_fqdn`/the box's
  own configured hostname, because `inventory_hostname` is guaranteed to
  already be the fully qualified name in this repo's real inventory
  (e.g. `aap1.lab.example.com`) — relying on the box's own DNS/
  `hostnamectl` configuration instead could silently keep producing a
  short name if that isn't itself set to the FQDN. Override per-host in
  inventory only if a host's `inventory_hostname` genuinely isn't its
  FQDN.

## TLS

See `roles/rsyslog_collector/README.md`'s "TLS" section for the full
picture (self-signed lab CA, server-only, `rsyslog-gnutls` package). On
this side, when `rsyslog_tls_enabled: true`:

1. Installs `rsyslog-gnutls`.
2. Fails fast (before touching config) if the collector's CA certificate
   isn't visible via `hostvars` yet - this catches TLS being enabled on a
   client but not (or not yet) on the collector, or the roles running in
   the wrong order.
3. Writes the collector's CA certificate to `rsyslog_tls_ca_cert_path`.
4. Deploys the forwarding config with `StreamDriverAuthMode="x509/name"`
   and `StreamDriverPermittedPeers` set to `rsyslog_tls_permitted_peer`,
   so this client both verifies the collector's certificate chains back to
   the trusted CA and matches the expected name - not just "signed by a
   CA I trust".
5. Sets `gnutlsPriorityString="{{ rsyslog_tls_priority_string }}"` on the
   same `omfwd` action, floor-restricting the negotiated protocol to TLS
   1.2+ (matches the floor enforced on the collector's `imtcp` listener).

## Usage

```yaml
- name: Configure log-forwarding clients
  hosts: log_clients
  become: true
  roles:
    - role: rsyslog_client
```

## Verifying journald persistence

Every run reports (non-disruptively) how many boots' worth of journal
history each client currently has. This role does not reboot hosts itself
to prove persistence — the next time a host is rebooted for any other
reason (patching, maintenance, etc.), a subsequent run's report will
naturally show more than one boot of history if persistence is working.

## Notes / next steps

- **Security**: matches `roles/rsyslog_collector` — plain TCP by default
  in this role's own `defaults/main.yml`, with server-only TLS
  (self-signed lab CA, `rsyslog-gnutls`, TLS 1.2+ floor via
  `rsyslog_tls_priority_string`) turned on fleet-wide by this repo's
  `group_vars/all/rsyslog_tls.yml`. See "TLS" above.
- If `log_clients` also needs the exclusion behaviour on the collector
  side, that's already handled there (see `roles/rsyslog_collector`).
