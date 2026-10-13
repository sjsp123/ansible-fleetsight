# host_availability_watcher

Active, fleet-wide email alerting the moment a host stops responding
entirely - no ping reply AND no SSH port response - checked directly
from the collector itself, with no agent, log forwarding, or database
dependency on the monitored hosts at all. This role is only ever
deployed to `[log_collector]` (see `playbooks/centralized_logging.yml`).

## Why this is different from the other two alerting roles

`roles/service_failure_watcher` and `roles/log_alerter` are both
entirely **passive** - they only ever notice a problem because the
affected host is still alive enough to forward a log line about it to
the collector via `roles/rsyslog_client`. A host that is completely
unreachable (powered off, kernel panic, hard hang, unplugged network
cable, hypervisor issue, etc.) can never log anything about its own
outage, so neither of those roles can ever detect that scenario.

`host_availability_watcher` closes that gap with an **active
heartbeat**: it directly pings and TCP-connects to every monitored
host's SSH port from the collector itself, on a short systemd timer,
completely independent of whether logging/rsyslog/the network path for
log forwarding is even working.

## How it works

1. Every `host_availability_watcher_poll_interval_seconds` (60s by
   default), a systemd timer runs `host_availability_watcher.py` once
   on the collector.
2. For each host in `host_availability_watcher_hosts` (defaults to
   every host already in `[log_clients]`), it:
   - Runs `ping -c <PING_COUNT> -W <PING_TIMEOUT_SECONDS> <host>` and
     checks the exit code.
   - Opens a plain TCP connection to `<host>:<SSH_PORT>` (default port
     22) with a short timeout - this only confirms something is
     listening and accepting connections; it never attempts to
     authenticate, so no SSH credentials or keys are ever needed on
     (or stored on) the collector.
3. Each host's outcome is combined into one of three states:
   - **UP** - ping and SSH port both OK.
   - **DOWN** - both ping and the SSH port failed for
     `host_availability_watcher_consecutive_failures_threshold`
     consecutive polls in a row (2 by default, so a genuine outage is
     detected within about two poll intervals) - the host is
     effectively completely unreachable.
   - **SSH_UNREACHABLE** - ping is OK but the SSH port has failed for
     the same consecutive-poll threshold - the host itself is up, but
     sshd/the port is not responding (crashed service, firewall
     change, etc.). This is a genuinely different failure mode from a
     fully dead host, so it gets its own state and its own subject
     line rather than being lumped in with DOWN.
4. State (per-host consecutive failure counts, current state, and
   when that state began) is kept in a small JSON file, not a
   database - `host_availability_watcher_state_file`.
5. An email is sent **only when a host's state changes** - not on
   every poll while a state persists - so a prolonged outage sends
   exactly one "down" email, not one every 60 seconds. When the host
   comes back, a "back UP" email is sent too (see
   `host_availability_watcher_notify_recovery` to disable that half).
   If several hosts change state in the same poll window (e.g. a
   shared switch/segment goes down), all of them are batched into a
   single email rather than one per host.

## What this does not cover

- **ICMP or the SSH port being blocked by a firewall/security group
  while the host is otherwise perfectly healthy** will still be
  reported as DOWN/SSH_UNREACHABLE - this role has no way to tell the
  difference between "host is dead" and "collector cannot reach it for
  network-policy reasons." Both are still operationally worth knowing
  about, but the wording in the alert email reflects "unreachable
  from the collector," not a certainty that the host itself has
  crashed.
- **This role alone does not tell you *why* a host is down** - just
  that it stopped responding. Once you're back on the console/iDRAC/
  iLO/hypervisor, the usual troubleshooting (root cause) is on you.
- **A dead collector itself has nothing to alert with** - if
  `admin1` (or whichever host runs this role) is the one that goes
  down, there is no mechanism left running to notice or email about
  it. Consider a genuinely external/off-box heartbeat (a different
  physical host, a cloud uptime checker, etc.) if you need coverage
  for the collector's own availability too - out of scope for this
  role.

## Mail delivery

The poller always talks to the LOCAL mail transport agent on
`host_availability_watcher_smtp_host` (the collector's own MTA, e.g.
admin1's Postfix) on port 25 - never an external SMTP relay directly,
so no relay credentials are ever stored in Ansible or code, matching
`roles/service_failure_watcher` and `roles/log_alerter`. Configure
that local MTA to actually relay outward however your organization
requires - entirely outside this role's scope.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `host_availability_watcher_enabled` | `false` | Opt-in switch (flipped to `true` fleet-wide by `group_vars/all/host_availability_watcher.yml`) |
| `host_availability_watcher_poll_interval_seconds` | `60` | How often the timer runs the poller |
| `host_availability_watcher_consecutive_failures_threshold` | `2` | Consecutive failed polls required before a state change is considered real and alerted on (guards against a single blip) |
| `host_availability_watcher_ping_count` / `_ping_timeout_seconds` | `2` / `2` | ICMP echo count and per-echo timeout |
| `host_availability_watcher_ssh_port` / `_ssh_timeout_seconds` | `22` / `3` | TCP port checked and its connect timeout - never attempts to authenticate |
| `host_availability_watcher_hosts` | every host in `[log_clients]` | Which hosts to actively check from the collector |
| `host_availability_watcher_notify_recovery` | `true` | Also email when a host returns to UP, not just when it goes down/degrades |
| `host_availability_watcher_state_dir` / `_state_file` | `/var/lib/host_availability_watcher` / `.../state.json` | Where per-host state is persisted between runs |
| `host_availability_watcher_user` | `svc_host_watch` | Unprivileged system account the timer runs as |
| `host_availability_watcher_app_dir` | `/opt/host_availability_watcher` | Where `config.py`/`host_availability_watcher.py` are deployed |
| `host_availability_watcher_email_to` | `root` | Recipient address - override to a real, deliverable address |
| `host_availability_watcher_email_from` | `rhel-alerts@<fqdn>` | From address |
| `host_availability_watcher_smtp_host` / `_smtp_port` | `localhost` / `25` | Local MTA the poller hands mail to |

## Testing it

On any monitored host, simulate an outage by firewalling off both
checks (safer than actually powering the host off, and reversible from
the same session):

```bash
sudo firewall-cmd --add-rich-rule='rule protocol value="icmp" drop' --timeout=180
sudo firewall-cmd --add-rich-rule='rule port port="22" protocol="tcp" drop' --timeout=180
# wait for host_availability_watcher_consecutive_failures_threshold
# polls (2 x 60s = ~2 minutes by default), then check the mailbox at
# host_availability_watcher_email_to - a DOWN alert should arrive, and
# a "back UP" alert once the 180s --timeout rules expire on their own.
```

To exercise just the SSH_UNREACHABLE path (host stays pingable, only
the SSH port goes away):

```bash
sudo systemctl stop sshd
# wait ~2 minutes, confirm an SSH_UNREACHABLE alert arrives, then:
sudo systemctl start sshd
# confirm a "back UP" alert arrives on the next poll.
```

A mocked, no-network unit test suite covering state classification,
the consecutive-failure threshold/hysteresis, the recovery alert, the
`NOTIFY_RECOVERY` toggle, and multi-host batching lives at
`tests/test_host_availability_watcher_logic.py` (never deployed - only
`files/host_availability_watcher.py` is copied to hosts):

```bash
python3 roles/host_availability_watcher/tests/test_host_availability_watcher_logic.py
```

## Rollout

Part of `playbooks/centralized_logging.yml`, gated by
`host_availability_watcher_enabled` (on by default via
`group_vars/all/host_availability_watcher.yml`), targeting
`[log_collector]` only:

```bash
ansible-playbook playbooks/centralized_logging.yml --limit admin1.lab.example.com
```

When `host_availability_watcher_enabled` is `false`, the same playbook
run stops and disables `host-availability-watcher.timer` if it already
exists, matching the teardown pattern already used for
`roles/log_alerter`/`roles/service_failure_watcher`.
