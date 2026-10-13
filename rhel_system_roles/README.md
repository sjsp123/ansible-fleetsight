# Time synchronization: Red Hat System Roles timesync

Chrony/NTP across the fleet is deployed via the upstream
`redhat.rhel_system_roles.timesync` role
(`playbooks/deploy_timesync_role.yml`) - the Red-Hat-maintained,
RHEL-native way to manage chrony/NTP, kept up to date across RHEL
releases by Red Hat rather than by a template in this repo.

This repo's previous own-template deployment path
(`templates/chrony.conf.j2`, `playbooks/deploy_chronyd.yml`,
`playbooks/copy_idm_chrony_conf.yml`, and the
`ansible_deploy_chronyd.sh` wrapper) has been **removed** - the system
role is the only supported way to (re)deploy chrony configuration now.
The tradeoff it brings is one extra collection dependency
(`redhat.rhel_system_roles`, shipped as the AppStream
`rhel-system-roles` RPM, not a Galaxy download - see "Installing the
collection" below).

## What is unaffected

- `roles/chrony_check/` (local NTP/chrony health checking) and
  `roles/chrony_watcher/` (centralized email alerting on breaches) are
  **unaffected** - they check whatever chrony configuration is actually
  running, regardless of which path deployed it.
- NTP servers come from each host's effective `chrony_ntp_servers`
  YAML list. `group_vars/all/site_hosts.yml` supplies the environment
  default; `group_vars/<realm_group>/site_hosts.yml` can override it
  per realm. The `timesync_ntp_hostnames` default in
  `group_vars/all/timesync.yml` reuses that resolved list.

## Running it

```bash
ansible-playbook playbooks/deploy_timesync_role.yml
# prompted for target_host - enter a single host or inventory group
```

Idempotent and safe to re-run: it converges `/etc/chrony.conf` to the
role's configuration with the NTP server list above and restarts
`chronyd` only when something actually changed. After running,
confirm `roles/chrony_check` still reports healthy sync on the
affected hosts.

## Variables

Set in `group_vars/all/timesync.yml`:

- `timesync_ntp_hostnames` - reuses the effective `chrony_ntp_servers`
  list, including a group-specific override when configured. Existing
  hosts using only `group_vars/all/site_hosts.yml` need no changes.

`playbooks/deploy_timesync_role.yml` converts
`timesync_ntp_hostnames` (a plain list of hostnames) into the
`{hostname, iburst}` dict list the role itself expects as its own
`timesync_ntp_servers` variable.

## Per-realm NTP configuration

Use inventory groups for membership and group-specific YAML files for
the server lists. Do not put comma-separated server names into an INI
`:vars` block: that produces a string, and the global YAML file can
override inline inventory variables rather than the other way round.

In the local `inventory` file, add the appropriate groups while
preserving existing host memberships:

```ini
[realm_one]
idm01.corp.example.com
app01.corp.example.com

[realm_two]
idm01.other.example.com
```

From `~/ansible`, create the two local directories:

```bash
cd ~/ansible
mkdir -p group_vars/realm_one group_vars/realm_two
```

Create or carefully edit `group_vars/realm_one/site_hosts.yml`:

```yaml
---
chrony_ntp_servers:
  - time.corp.example.com
  - time1.corp.example.com
```

Create or carefully edit `group_vars/realm_two/site_hosts.yml`:

```yaml
---
chrony_ntp_servers:
  - time.other.example.com
  - time1.other.example.com
```

Replace the illustrative hosts and server names with the appropriate
environment values. Do not overwrite an existing local file without
backing it up. `group_vars/realm_one/site_hosts.yml.example` is a
reference only; Ansible does not load the `.example` file.

Each host should belong to only one realm group defining this list.
Group-specific YAML overrides the global default; explicit host-level
or extra-variable overrides can still take precedence. The existing
public-pool fallback is unchanged if no `chrony_ntp_servers` value is
defined, so explicitly configure approved internal servers for isolated
environments.

Inspect the resolved list without applying changes:

```bash
cd ~/ansible
ansible realm_one -m ansible.builtin.debug -a 'var=timesync_ntp_hostnames'
ansible realm_two -m ansible.builtin.debug -a 'var=timesync_ntp_hostnames'
```

Confirm that each result is a list containing only that realm's intended
servers, then check a single host first:

```bash
ansible-playbook playbooks/deploy_timesync_role.yml \
  -e target_host=realm_one --limit idm01.corp.example.com --check --diff
```

Use the existing approved deployment process to apply a reviewed result,
then verify synchronization on that host before widening the target.
Validation rejects empty lists, strings, mappings, and invalid list
items before the role runs. A validation failure stops the targeted
play rather than allowing other hosts to proceed with a partial rollout.

### Local regression checks

With `ansible-playbook` and PyYAML available in the test environment:

```bash
python3 -m unittest discover -s rhel_system_roles/tests -v
```

These tests use temporary inventories and only local
assert/set_fact/debug tasks. They verify realm precedence, unchanged
defaults, invalid-input rejection, and repeated preparation without
calling the system role or changing any host's time configuration.

## Installing the collection

**`redhat.rhel_system_roles` is not on public `galaxy.ansible.com`** -
there's no `redhat` namespace there at all, so
`ansible-galaxy collection install/download -r requirements.yml` cannot
fetch it (confirmed directly: the Galaxy API returns zero results for
that namespace, and a download attempt fails with a dependency
resolution error). It's distributed two other ways:

1. **Via the RHEL AppStream `rhel-system-roles` RPM** (used by this
   repo, since AppStream is already a hard requirement for everything
   else here - zero extra repos, stays dnf-update maintainable):

   ```bash
   sudo dnf install rhel-system-roles
   ```

   This installs the collection into this repo's own `./collections` -
   nothing else to configure. Run it on the Ansible control node itself
   (not the managed hosts).

2. **Via Red Hat Automation Hub** (`console.redhat.com` /
   `cloud.redhat.com`), if you'd rather manage it as a versioned Galaxy
   collection - needs a Red Hat subscription and a Hub API token
   configured in `ansible.cfg`/`ansible-galaxy.cfg`. Not used by this
   repo; documented here only as the alternative.

**No internet access on the control node, and no Satellite/CDN mirror
for the AppStream repo either?** `requirements.yml` deliberately does
**not** list `redhat.rhel_system_roles` as a Galaxy dependency (see the
comment there), so it's also not part of
[`offline_collections/README.md`](../offline_collections/README.md)'s
tarball bundle - that mechanism only applies to actual Galaxy
collections. Instead, download the RPM (and its dependencies) on a
connected machine and copy it over:

```bash
# On a connected machine with the same RHEL major version:
dnf download --resolve --destdir ./rhel-system-roles-rpm rhel-system-roles
# Copy ./rhel-system-roles-rpm/*.rpm to the offline control node, then:
sudo dnf install ./rhel-system-roles-rpm/*.rpm
```
