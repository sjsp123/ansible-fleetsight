# silence_setroubleshoot

Silence `setroubleshootd`/`sealert` AVC notification noise on RHEL PoC and
home-lab hosts, and set SELinux to **Permissive** so denials are logged but
not enforced.

**Permissive vs. Disabled:** this role deliberately does NOT disable SELinux.
Permissive keeps AVCs flowing into `/var/log/audit/audit.log` (so you retain
forensic evidence of what would have been denied), it just stops enforcement.
It also avoids a reboot — `enforcing ↔ permissive` is a live kernel switch
via `setenforce(1)`; only the `enforcing/permissive → disabled` transition
requires a reboot.

## What this role does

1. **Silences the setroubleshoot notification pipeline** — sets
   `active = no` in the sedispatch audit plugin config
   ([Red Hat Solution 2762771](https://access.redhat.com/solutions/2762771))
   and restarts `auditd`. AVCs still land in `/var/log/audit/audit.log`;
   they just stop being fed to `setroubleshootd` and stop generating
   `sealert` entries in the journal.
2. **Sets SELinux to Permissive** — both at runtime (`setenforce 0` via the
   `ansible.posix.selinux` module) and persistently (`SELINUX=permissive` in
   `/etc/selinux/config`). No reboot required.
3. **Warns if the kernel command line is out of sync** — checks for stale
   `enforcing=1` or `selinux=0` args on the default kernel that would
   override `/etc/selinux/config` on the next reboot, and prints the
   `grubby` command to strip them.

## References

- [Red Hat Solution 2762771 — How to disable setroubleshootd](https://access.redhat.com/solutions/2762771) — the sedispatch fix, Solution Verified, updated 10 Mar 2026.
- [Red Hat Solution 1523643 — dontaudit rules](https://access.redhat.com/solutions/1523643) — targeted alternative if you'd rather stay enforcing and silence specific denials.
- [Red Hat Solution 3412221 — aureport shows denied messages](https://access.redhat.com/solutions/3412221) — AVC lifecycle explainer.
- [Using SELinux (RHEL 10) — Ch. Changing SELinux states and modes](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_selinux/) — authoritative source for permissive/enforcing/disabled transitions and reboot semantics.

## Scope guard

**This role should NEVER run against enforcing production hosts.** The
playbook prompts for the target group (default: `all`, matching
`playbooks/hostinfo.yml`'s convention) and asserts each resolved host is in
the `homelab`, `work`, or `lab` group — that assert, not the prompt default,
is what actually keeps this off production. `lab` is accepted as a third,
equally valid group name alongside `homelab`/`work`, since some inventories
use it as their own local convention for non-collector home-lab hosts; the
guard cares about non-production scope, not which group name you picked for
it. If you press Enter and your inventory has production hosts alongside
homelab/work/lab ones, those production hosts fail the assert individually
and are skipped; the recognized non-production hosts still run. The role
also refuses to run against hosts whose `group_names` contain `production`
or `prod`, as a second, independent check.

The role additionally refuses to run against a host that is currently
`Disabled`, because Disabled → Permissive requires a reboot.

## Variables

| Variable | Default | Effect |
|---|---|---|
| `silence_setroubleshoot_disable_sedispatch` | `true` | Set `active = no` in sedispatch.conf and restart auditd. |
| `silence_setroubleshoot_set_permissive` | `true` | `setenforce 0` at runtime AND `SELINUX=permissive` in `/etc/selinux/config`. |

## Version coverage

| RHEL | Sedispatch path | KB 2762771 cited | Notes |
|---|---|---|---|
| 7 | `/etc/audisp/plugins.d/sedispatch.conf` | ✓ | `service auditd restart` (upstart/systemd wrapper) |
| 8 | `/etc/audit/plugins.d/sedispatch.conf` | ✓ | `service auditd restart` (bypasses RefuseManualStop) |
| 9 | `/etc/audit/plugins.d/sedispatch.conf` | ✓ | Same as RHEL 8 |
| 10 | `/etc/audit/plugins.d/sedispatch.conf` | **No (inferred)** | Verify on lab; consider Red Hat case to extend KB |

## Usage

The playbook uses the same `vars_prompt` pattern as `playbooks/hostinfo.yml`
(prompt text and `default: "all"` both match exactly): run it with no
`-e target_host=...` and it prompts you for the inventory group or host
(default: `all`, press Enter to accept — the scope guard still restricts
actual execution to `homelab`/`work`/`lab` members regardless of what you
type). Pass `-e target_host=...` explicitly for unattended/cron runs so
`vars_prompt` is skipped.

The same playbook file lives on both branches. On `main` you'll typically run
it against `homelab` or `lab` (whichever group your real inventory uses for
non-collector home-lab hosts); on `work` you'll run it against `work`. The
scope guard accepts any of the three.

```bash
# Interactive — prompts you for the target (default: all)
ansible-playbook playbooks/silence_setroubleshoot.yml

# Interactive dry run
ansible-playbook playbooks/silence_setroubleshoot.yml --check --diff

# Unattended — whole homelab group
ansible-playbook playbooks/silence_setroubleshoot.yml -e target_host=homelab

# Unattended — whole work group
ansible-playbook playbooks/silence_setroubleshoot.yml -e target_host=work

# Unattended — whole lab group (if your inventory uses that name instead)
ansible-playbook playbooks/silence_setroubleshoot.yml -e target_host=lab

# Unattended — a single host
ansible-playbook playbooks/silence_setroubleshoot.yml \
  -e target_host=admin1.lab.example.com
```

## Reversal

```bash
# Re-enable sedispatch notifications (RHEL 8+; RHEL 7 uses /etc/audisp/)
sed -i "s/^active = no/active = yes/" /etc/audit/plugins.d/sedispatch.conf
service auditd restart

# Return SELinux to enforcing (runtime + persistent, no reboot)
setenforce 1
sed -i "s/^SELINUX=permissive/SELINUX=enforcing/" /etc/selinux/config
getenforce      # → Enforcing
```

## Requirements

- `ansible.posix` collection (already in this repo's `requirements.yml` for
  the `selinux` module). Install with:

  ```bash
  ansible-galaxy collection install -r requirements.yml
  ```
