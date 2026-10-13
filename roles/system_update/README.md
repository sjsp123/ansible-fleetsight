# Role: system_update

## Purpose

Idempotent patch management across the fleet — refreshes package
metadata, upgrades packages, optionally removes now-unneeded
dependencies, and reboots only if the running kernel/libraries actually
need it. Uses the `ansible.builtin.dnf`/`ansible.builtin.yum` modules
directly (no shell subprocess, no `which` probing), branching on the
`ansible_pkg_mgr` fact Ansible already gathers — the same role works
unmodified on RHEL 7 (`yum`) through RHEL 8/9/10 (`dnf`).

Everything comes from RHEL's own BaseOS/AppStream repos (`dnf-utils`/
`yum-utils`, needed for `needs-restarting`, plus whatever's already
installed) — no EPEL, no third-party repos.

## Behaviour

1. Fails fast (`ansible.builtin.assert`) if `ansible_pkg_mgr` isn't
   `dnf` or `yum` — this role doesn't support anything else.
2. Ensures `dnf-utils`/`yum-utils` is present (provides
   `needs-restarting`, used later to decide whether a reboot is
   actually required).
3. Refreshes package metadata, then lists (and prints via `debug`)
   packages with pending updates *before* touching anything.
4. Upgrades all packages to latest (`security: true` restricts this to
   security errata only, when `system_update_security_only` is set),
   honoring `system_update_exclude` to hold specific packages back.
   Prints exactly what changed.
5. Optionally runs autoremove (`system_update_autoremove: true`) to
   drop packages no longer required after the upgrade — gated by the
   `when` condition, so this task doesn't even run unless you asked for
   it. (Fixed as part of a repo review: the module call itself
   previously hardcoded `autoremove: false`, meaning even a `true`
   setting would never remove anything — it's now `autoremove: true`,
   consistent with the surrounding `when` gate.)
6. Runs `needs-restarting -r` to determine whether a reboot is actually
   required (not just "packages changed") and records the result as
   `system_update_reboot_required`.
7. Reboots only if both `system_update_reboot_required` and
   `system_update_reboot_enabled` are true, using
   `ansible.builtin.reboot` with configurable timeouts/delays and a
   post-reboot test command (waits for the host to come back and
   confirm `system_update_test_command` succeeds before moving on).
8. Prints a final per-host summary: whether packages changed, whether a
   reboot was required, and whether one was actually performed.

## Requirements

`ansible_pkg_mgr` must resolve to `dnf` or `yum` (true for every
supported RHEL release in this fleet). No packages beyond
`dnf-utils`/`yum-utils` are installed by this role itself.

## Usage

```bash
ansible-playbook playbooks/update_system.yml -e target_hosts=client1
```

`hosts: "{{ target_hosts | mandatory }}"` in `playbooks/update_system.yml`
means `target_hosts` **must** be supplied via `-e` — there's no
interactive prompt and no default, so an unattended/cron run can never
hang waiting on one (see the root `ansible_update_system.sh` wrapper,
which prompts interactively for both the target and whether to reboot,
then passes both through as `-e`).

Useful overrides:

```bash
# Patch only, never reboot (e.g. to stage kernels for a maintenance window)
ansible-playbook playbooks/update_system.yml -e target_hosts=all -e system_update_reboot_enabled=false

# Security errata only
ansible-playbook playbooks/update_system.yml -e target_hosts=all -e system_update_security_only=true

# One host at a time instead of the default 30% batch
ansible-playbook playbooks/update_system.yml -e target_hosts=all -e update_serial=1

# Patch now, defer any reboot to later
ansible-playbook playbooks/update_system.yml -e target_hosts=all --skip-tags reboot
```

## Variables

- `system_update_reboot_enabled` (bool, default `true`) — set `false`
  to patch without ever rebooting.
- `system_update_security_only` (bool, default `false`) — restrict the
  upgrade to security errata only (requires subscription-manager
  entitlement data to classify packages).
- `system_update_autoremove` (bool, default `false`) — run
  autoremove after the upgrade to drop now-unneeded dependencies.
- `system_update_exclude` (list, default `[]`) — packages/globs to hold
  back from the upgrade, e.g. `["kernel*", "docker-ce"]`.
- `system_update_cache_valid_time` (int, default `3600`) — informational
  only; metadata is always refreshed explicitly regardless.
- `system_update_reboot_timeout` / `system_update_reboot_pre_delay` /
  `system_update_reboot_post_delay` / `system_update_reboot_connect_timeout`
  (ints, defaults `600`/`5`/`30`/`5`) — tuning passed straight through
  to `ansible.builtin.reboot`.
- `system_update_test_command` (string, default `uptime`) — command the
  `reboot` module runs post-reboot to confirm the host is really back up
  before the play continues.
- `update_serial` (string, default `"30%"`, set at the play level in
  `playbooks/update_system.yml`) — how many hosts to patch/reboot at
  once; not a role default, override with `-e update_serial=...`.

## Tags

Every task is tagged `update`; the reboot-related tasks are additionally
tagged `reboot`. `--skip-tags reboot` patches without ever rebooting for
that run, regardless of `system_update_reboot_enabled`.
