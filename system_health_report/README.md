# System Health Check and Configuration Report (Ansible)

Ansible replacement for the Python script that used `psutil`, `distro`, and
`tabulate`. This version relies only on `ansible.builtin` modules and
standard Ansible facts, so **no Python packages need to be installed on the
target hosts** — just Python 3 (already required by Ansible itself).

## Files

- `playbooks/system_health_report.yml` — the playbook (single play, works against any number of hosts at once).
- `playbooks/templates/report.txt.j2` — Jinja2 template that renders the report.
- `playbooks/reports/` — output folder; a `<hostname>_system_health_report.txt` file is saved here after each run. The playbook resolves this as `{{ playbook_dir }}/reports`, so it always lands next to the playbook no matter which directory you run `ansible-playbook` from.

There's no bundled example inventory — point `-i` at your own inventory
file (e.g. the one already used by your other playbooks in `~/ansible`)
or an ad hoc host list.

## Dropping this into an existing `~/ansible` layout

If your playbooks already live in `~/ansible/playbooks/` (with `~/ansible/ansible.cfg`, `~/ansible/inventory`, etc.), place the files exactly like your other standalone playbooks (e.g. `hostinfo.yml`, `sudo_audit_report.yml`) that keep their own `templates/`/`files/` next to them:

```
ansible/
├── ansible.cfg
├── inventory
└── playbooks/
    ├── system_health_report.yml   <- the playbook
    ├── templates/
    │   └── report.txt.j2          <- referenced by the playbook as templates/report.txt.j2
    └── reports/                  <- created automatically; safe to .gitignore
```

The `template` module already searches a `templates/` directory next to the playbook first (the same convention your `playbooks/files/...` folders rely on for `copy`), so no path changes are needed — just copy `templates/report.txt.j2` to `playbooks/templates/report.txt.j2` and `system_health_report.yml` to `playbooks/system_health_report.yml`. Run it the same way you run your other playbooks, e.g. from `~/ansible`:

```bash
cd ~/ansible
ansible-playbook -i inventory playbooks/system_health_report.yml
```

## What it collects

| Section | Source |
|---|---|
| Hostname / FQDN / Uptime / Load average | `ansible_hostname`, `ansible_fqdn`, `uptime -p`, `/proc/loadavg` |
| OS version / kernel | `ansible_distribution*`, `ansible_kernel` |
| CPU (sockets, cores, vCPUs, model) | `ansible_processor*` facts |
| Memory (total/free/used/%, swap) | `ansible_memtotal_mb`, `ansible_memfree_mb`, `ansible_swap*_mb` |
| Disk (per mount: total/used/free/%, fstype) | `ansible_mounts` |
| Network (cumulative bytes/packets sent+received, excl. loopback) | `/proc/net/dev` parsed with `awk` |
| Critical services (`sshd`, `firewalld`, `multipathd` by default) | `ansible.builtin.service_facts` |

## Usage

```bash
# Point at your existing inventory (e.g. the one your other playbooks use)
ansible-playbook -i inventory system_health_report.yml

# Add --become if a target requires elevated privileges to read service state
ansible-playbook -i inventory system_health_report.yml --become

# Check a different set of critical services
ansible-playbook -i inventory system_health_report.yml -e '{"critical_services": ["sshd","chronyd","rhsmcertd"]}'

# Run against a single host (e.g. localhost, adhoc)
ansible-playbook -i localhost, -c local system_health_report.yml
```

Each run prints the report to the console for every host (like the original
script's stdout output) and also writes a copy to `reports/` on the control
node, so you keep a per-host, per-run artifact for your operational
reporting — handy to drop into a report directory, attach to a ticket, or
diff between runs.

## Notes / adapting further

- If a checked service isn't installed on a host, its status shows as
  `not-found` instead of erroring out.
- CPU "usage percent per core" from the original script needs a sampling
  interval (`psutil.cpu_percent`); this isn't available as a static Ansible
  fact. The playbook reports 1/5/15-minute load averages instead, which is
  the standard Ansible/Linux equivalent for a point-in-time health check. If
  you need live per-core percentages, add a `mpstat -P ALL 1 1` task (requires
  the `sysstat` package on targets) and extend the template.
- To turn this into a reusable role for your AAP/AWX job templates, move
  `playbook.yml`'s tasks into `roles/system_health_report/tasks/main.yml` and
  the template into `roles/system_health_report/templates/`.
