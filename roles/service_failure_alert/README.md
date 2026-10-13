# service_failure_alert

Real-time alerting for systemd unit failures - the moment a covered
unit enters the `failed` state, an email fires immediately. This
closes a specific gap: `hostinfo`/`linuxdb_reports`/`svr_check.sh` only
notice a stopped/failed service the next time their cron run happens,
and even then only surface it once someone opens the dashboard. This
role uses systemd's own `OnFailure=` mechanism instead, so detection is
instant and does not depend on any cron schedule, polling interval, or
someone happening to look at the dashboard.

## How it works

1. This role adds a drop-in (`/etc/systemd/system/<unit>.d/service-failure-alert.conf`)
   to every covered unit, containing:
   ```
   [Unit]
   OnFailure=service-failure-alert@%n.service
   ```
   `%n` expands to the full unit name of the unit that just failed
   (e.g. `chronyd.service`), so this activates a *new*, per-failure
   instance of the templated `service-failure-alert@.service` unit
   this role also deploys - its instance name ends up being the
   literal string `chronyd.service` (dots are valid inside a systemd
   instance name), which the templated unit passes straight through
   to the alert script via `%I`.
2. `service-failure-alert@.service` is a `Type=oneshot` unit whose
   `ExecStart` runs `/usr/local/sbin/service_failure_alert.py <unit>`.
3. The script runs `systemctl status --no-pager -l <unit>` for
   context, then sends one email through the **local** mail transport
   agent (see "Mail delivery" below) with the failed unit, hostname,
   and that status output in the body.

This is genuinely real-time - there is no polling loop and no delay
beyond however long the alert script itself takes to run (typically
well under a second).

## What's covered by default

- `chronyd.service` on **every** host (`service_failure_alert_always_units`)
  - this is the exact scenario that motivated this role: stopping
    chronyd and wanting to know immediately, not at the next
    `hostinfo` run.
- `rsm222.service` on hosts in `rsm222_hosts`, `rsm232.service` on
  hosts in `rsm232_hosts` - reusing the SAME host-group lists
  `group_vars/all/host_checks.yml`'s own RSM222/RSM232 checks use
  (`group_vars/all/site_hosts.yml`), so there's one source of truth
  for "which hosts run RSM222/232", not a second list to keep in sync.

Add more units fleet-wide via `service_failure_alert_extra_units` (a
plain list of unit names, e.g. `["sshd.service", "firewalld.service",
"auditd.service", "crond.service"]` - all four are already tracked by
`svr_check.sh`'s `FAILED_SVC_CNT` column, just not previously wired to
alert the instant they fail rather than at the next
`linuxdb_reports` cron run).

## What this does not cover

**FLEXLM is NOT covered.** `group_vars/all/host_checks.yml` checks it
with `pgrep -f 'lmgrd'`, not `systemctl is-active` - there is no
systemd unit and therefore no systemd "failed" state to hook
`OnFailure=` onto. If FlexLM's `lmgrd` is ever wrapped in its own
systemd service on your hosts, add that unit's name to
`service_failure_alert_extra_units` and it will be covered like any
other unit. Until then, a stopped FlexLM is still caught by the
existing dashboard traffic light/cell highlighting - just at the next
`hostinfo` run, not instantly.

Similarly, this role only detects **unit failures**, not resource
thresholds (disk filling up, load spiking, etc.) - those still need
either the next scheduled check or a separate short-interval polling
mechanism.

## Mail delivery

The alert script always sends through the **local** mail transport
agent (`localhost:25` by default) - it never talks to an external SMTP
relay and never handles relay credentials. Getting mail from
`localhost:25` onward to a real inbox is a host-level prerequisite,
not something this role manages:

- **Home lab**: already working (Postfix on the collector already
  relays outbound through Gmail).
- **Any other deployment**: needs a local Postfix installed and pointed
  at your mail relay first - a one-time manual host setup step, e.g.:
  ```
  sudo dnf install -y postfix
  sudo sed -i "s/^relayhost =.*/relayhost = [mailrelay.yourcorp.internal]:25/" /etc/postfix/main.cf
  sudo systemctl enable --now postfix
  ```
  (adjust the relay address/port to your actual internal relay, and
  add SASL auth lines to `/etc/postfix/main.cf` + `sasl_passwd` if
  your relay requires a username/password - see Postfix's own
  `SASL_README` on the host, `man 5 postfix-sasl`).

`service_failure_alert_email_to` defaults to `root`, on the assumption
that root's mail is already routed somewhere useful on the host
(`/etc/aliases`, or the mail setup above). Override it to a real
address (in `group_vars/all/service_failure_alert.yml` or per-host) if
that's not the case.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `service_failure_alert_enabled` | `false` | Opt-in toggle (this role's own default). See `group_vars/all/service_failure_alert.yml` for the fleet-wide override. |
| `service_failure_alert_always_units` | `[chronyd.service]` | Units covered on every host. |
| `service_failure_alert_extra_units` | `[]` | Additional units covered on every host - extend this, don't edit `_always_units`. |
| `service_failure_alert_email_to` | `root` | Alert email recipient. |
| `service_failure_alert_email_from` | `rhel-alerts@<fqdn>` | Alert email sender. |
| `service_failure_alert_smtp_host` / `_smtp_port` | `localhost` / `25` | Local MTA the script talks to. |
| `service_failure_alert_script_path` | `/usr/local/sbin/service_failure_alert.py` | Where the alert script is installed. |

## Testing it

```
sudo systemctl stop chronyd
```

Within a few seconds you should receive an email titled
`[<hostname>] chronyd.service FAILED`. Confirm the drop-in and
template unit are in place first if it doesn't arrive:

```
systemctl cat chronyd.service   # should show OnFailure=... from the drop-in
systemctl cat service-failure-alert@chronyd.service.service
journalctl -u 'service-failure-alert@*' --no-pager
```

Restart chronyd afterwards:

```
sudo systemctl start chronyd
```

(Restarting chronyd does not clear the FAILED state systemd recorded
for the OnFailure= trigger that already fired - that's expected and
harmless; the next failure will trigger a fresh alert regardless.)

## Rollout

Add to inventory as usual (this role targets every host that should be
covered - typically `log_clients`, same group `hostinfo`/
`linuxdb_reports` already use):

```
ansible-playbook playbooks/service_failure_alert.yml
```
