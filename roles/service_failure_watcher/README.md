# service_failure_watcher

Centralized, fleet-wide email alerting the moment **any** systemd unit
on **any** host enters the failed state - without installing a mail
relay on every host. This role is only ever deployed to
`[log_collector]` (see `playbooks/centralized_logging.yml`); it reads
the existing `log_db` database (`roles/log_db`, fed by
`roles/rsyslog_collector`) and relies on `roles/rsyslog_client` already
forwarding every managed host's journald content to the collector.

It supersedes `roles/service_failure_alert`, which wired a systemd
`OnFailure=` drop-in into each covered unit on **every** host and
required a working local mail transport agent there too. That role is
now retired and disabled by default - see root `README.md` "Alerting"
for the full reasoning.

## How it works

1. `roles/rsyslog_client` already forwards every host's local +
   journald log stream to the collector (`90-remote-forwarding.conf.j2`
   - no client-side severity filtering at all).
2. The instant any systemd unit enters the failed state, systemd itself
   logs one fixed-wording line to the journal:
   `"<unit>: Failed with result '<reason>'."` - e.g.
   `"chronyd.service: Failed with result 'signal'."` This wording is
   the same across virtually every unit type (service/socket/timer/
   mount/etc.) and every RHEL release, which is what makes it a single,
   reliable, fleet-wide detection signal.
3. That message is forwarded to the collector like any other journald
   line, and (being logged by systemd at `err` severity or worse)
   clears `roles/rsyslog_collector`'s DB severity filter by default, so
   it lands in `log_db`'s `systemevents` table - see
   `roles/rsyslog_collector/README.md` for that filter's exact rules.
4. This role's poller (`service_failure_watcher.py`) runs on a short
   systemd timer on the collector only, queries `log_db` for new rows
   matching `message LIKE '%: Failed with result %'`, parses out the
   failing unit name and result reason, and emails one batched summary
   through the collector's own local mail transport agent.
5. State (the id of the last row already alerted on) is kept in a
   small JSON file, not the database - the first-ever run bootstraps
   the watermark to "now" instead of emailing about every failure
   already sitting in the database.

## What this does not cover

- **FLEXLM** - checked via `pgrep`, not a systemd unit, so it never
  logs systemd's "Failed with result" message. Same documented gap as
  the retired `roles/service_failure_alert`.
- A host whose logs never reach the collector (network partition,
  rsyslog down on the client, etc.) obviously can't have its failures
  detected here either - this is a log-pipeline-dependent mechanism,
  not an independent heartbeat/liveness check.
- Detection lands within one poll interval
  (`service_failure_watcher_poll_interval_seconds`, default 30s), not
  instantly like the retired per-host `OnFailure=` hook was. In
  exchange, no per-host mail relay is needed at all.

## Mail delivery

The poller always talks to the LOCAL mail transport agent on
`service_failure_watcher_smtp_host` (the collector's own MTA, e.g.
admin1's Postfix) on port 25 - never an external SMTP relay directly,
so no relay credentials are ever stored in Ansible or code. Configure
that local MTA to actually relay outward however your organization
requires (a corporate smarthost, Microsoft 365, Gmail, etc.) -
entirely outside this role's scope.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `service_failure_watcher_enabled` | `false` | Opt-in switch (flipped to `true` fleet-wide by `group_vars/all/service_failure_watcher.yml`) |
| `service_failure_watcher_poll_interval_seconds` | `30` | How often the timer runs the poller |
| `service_failure_watcher_max_rows_per_email` | `50` | Cap on failures listed in one email if several land in the same poll window |
| `service_failure_watcher_state_dir` / `_state_file` | `/var/lib/service_failure_watcher` / `.../state.json` | Where the last-alerted watermark is kept |
| `service_failure_watcher_user` | `svc_fail_watch` | Unprivileged system account the timer runs as |
| `service_failure_watcher_app_dir` | `/opt/service_failure_watcher` | Where `config.py`/`service_failure_watcher.py` are deployed |
| `service_failure_watcher_db_host/_name/_table/_user/_password` | mirrors `log_db_*`/`rsyslog_collector_db_host` | Read-only DB connection - stays in sync automatically if you only override the `log_db_*` versions |
| `service_failure_watcher_email_to` | `root` | Recipient address - override to a real, deliverable address |
| `service_failure_watcher_email_from` | `rhel-alerts@<fqdn>` | From address |
| `service_failure_watcher_smtp_host` / `_smtp_port` | `localhost` / `25` | Local MTA the poller hands mail to |

## Testing it

On a host already in `[log_clients]` (not the collector itself),
deliberately crash a unit and confirm the alert arrives within one poll
interval:

```bash
sudo systemctl kill --signal=SIGKILL chronyd
# wait up to service_failure_watcher_poll_interval_seconds (30s default),
# then check the mailbox at service_failure_watcher_email_to.
```

To confirm the collector-side query logic directly instead of waiting
on a real crash, this row alone in `log_db.systemevents` is enough to
trigger an alert on the next poll:

```sql
SELECT id, message, fromhost FROM systemevents
WHERE message LIKE '%: Failed with result %'
ORDER BY id DESC LIMIT 5;
```

A mocked, no-database unit test suite covering the bootstrap, dedup,
row-cap, subject/parsing, and non-matching-row-exclusion behaviour
lives at `tests/test_service_failure_watcher_logic.py` (never deployed
- only `files/service_failure_watcher.py` is copied to hosts):

```bash
python3 roles/service_failure_watcher/tests/test_service_failure_watcher_logic.py
```

## Rollout

Part of `playbooks/centralized_logging.yml`, gated by
`service_failure_watcher_enabled` (on by default via
`group_vars/all/service_failure_watcher.yml`), targeting
`[log_collector]` only:

```bash
ansible-playbook playbooks/centralized_logging.yml --limit admin1.lab.example.com
```

When `service_failure_watcher_enabled` is `false`, the same playbook
run stops and disables `service-failure-watcher.timer` if it already
exists, matching the teardown pattern already used for
`roles/log_alerter`.
