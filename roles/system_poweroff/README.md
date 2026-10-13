# system_poweroff

Gracefully shuts down RHEL/Oracle Linux 7-10 hosts - physical HPE ProLiant
DL380 Gen10/Gen11 or virtual - and then confirms each one is *actually*
powered off, not just unreachable.

## How it works

1. **Safety asserts** (fail fast, before anything destructive):
   - `system_poweroff_confirm` must be explicitly `true` (always pass it via
     `-e`, never commit it as `true`).
   - Refuses to target the Ansible control node itself
     (`ansible_connection == local`).
   - Refuses to target an OS outside RHEL/Oracle Linux 7-10, unless
     `system_poweroff_skip_os_check: true`.
   - Warns (does not block) if a host is in `[hpe_ilo_hosts]` but
     `ansible_virtualization_role` says it is actually a guest - a sign the
     inventory grouping is wrong for that host.
2. **Physical hosts only** (`[hpe_ilo_hosts]`): captures the host's own
   iLO/BMC IP address *in-band*, while its OS is still up - `ipmitool lan
   print`, run locally, the same technique `playbooks/collect_ilo.yml`
   already uses. This has to happen *before* shutdown: once the OS is down
   there is no more in-band access, and the verification step needs the
   iLO's own network address to query it from outside.
3. **Shutdown**: `shutdown -h now` on the target, fired via `async: 1,
   poll: 0` (fire-and-forget) so Ansible does not treat the connection
   dropping mid-command as an error. Skippable with
   `system_poweroff_skip_shutdown: true`, to run just the verification step
   against a host you already shut down another way.
4. **Verification** (out-of-band, from `system_poweroff_verify_delegate_host`
   - the control node by default - never from the target itself):
   - **`[hpe_ilo_hosts]` (physical)**: polls the iLO/BMC directly over the
     network (its own management IP, captured in step 2) until it reports
     the chassis is powered off, or the timeout is reached.
   - **Everything else (virtual)**: polls the host's SSH port until it stops
     responding, or the timeout is reached.
   - Either way, a per-host `system_poweroff_result` fact
     (`{host, method, confirmed_off}`) is recorded and printed, and the host
     fails clearly if power-off was never confirmed.

## Choosing which hosts to target

`target_hosts` (a single host, comma-separated list, or inventory group
name such as `hpe_ilo_hosts`) can be supplied two ways:

- **Non-interactively**, e.g. from AAP/cron - pass it with `-e`, same as
  every other playbook in this repo:
  `-e target_hosts=hpe_ilo_hosts`. When supplied this way it is used
  as-is and you are never prompted.
- **Interactively**, running it by hand at a terminal - omit
  `-e target_hosts=...` and the playbook prompts for it:
  `Which inventory group or host(s) do you want to power off?`. This is
  a plain `vars_prompt` (not masked/hidden input), and it is
  automatically skipped whenever `-e target_hosts=...` is already set,
  so scripted/automated runs behave exactly as before.

`-e system_poweroff_confirm=true` is unaffected by this and is **never**
prompted for - it always has to be passed explicitly on the command
line, precisely so a plain `ansible-playbook playbooks/system_poweroff.yml`
with no flags can never shut anything down by itself (it just prompts
for a target, then fails at the confirm assert).

**Important:** `--list-hosts` only resolves the target from `-e
target_hosts=...` - it never runs the interactive prompt (it does not
execute any play at all, prompt included). Always use the `-e` form
when you want to preview the exact host list before running for real.

## Power-off order

Hosts are processed one at a time by default (`serial: 1`) and in the
order they appear in the inventory file for the targeted group -
Ansible's default play host-ordering is inventory-file order, not
alphabetical, and this role relies on that default rather than
overriding it (an explicit `order: inventory` keyword was tested and
rejected - it makes `--list-hosts`'s preview order disagree with the
actual run order, even though the real run order is correct either
way). To control the sequence servers power off in, reorder the hosts
under that group in your inventory file.

## Physical vs. virtual hosts

Handled purely by inventory group membership - no extra variable needed:
hosts in `[hpe_ilo_hosts]` (already documented in `inventory.example.ini`
for `playbooks/collect_ilo.yml`) get the BMC-based check; anything else
gets the network-based check.

## Choosing ipmitool vs. ilorest

Both are already-installed CLI tools this repo shells out to directly (no
Ansible collection, no Python IPMI/Redfish library) - the same
zero-new-dependency pattern `playbooks/collect_ilo.yml` uses, which matters
for keeping this portable into the disconnected work proof-of-concept.

| | `ipmitool` (IPMI-over-LAN / lanplus) | `ilorest` (HPE iLO Redfish) |
|---|---|---|
| Cost | Free, packaged in every RHEL/OL repo | Free, HPE-provided (`ilorest` RPM) |
| Install effort | `dnf install ipmitool` | Download/install HPE's `ilorest` RPM per HPE's repo |
| Output parsing | One line: `Chassis Power is off` - trivial and reliable to match | JSON payload - needs a `grep`/parse step, more brittle across `ilorest` versions |
| Credential handling | Password can go via the `IPMI_PASSWORD` environment variable (`-E`), keeping it off the process command line | Password is a CLI argument (`-p`) - visible in `ps` on the delegate host for the task's duration |
| Reliability going forward | HPE has been steadily de-emphasizing IPMI-over-LAN in favor of Redfish, and some hardened Gen11 configurations disable it entirely | HPE's actively maintained, recommended long-term interface for iLO 5/6 |
| Maintenance burden | Lower - simpler output, one binary | Slightly higher - watch for `ilorest` CLI syntax changes across major versions |

**Default: `ipmitool`.** Set `system_poweroff_bmc_tool: ilorest` (fleet-wide
in `group_vars/all/system_poweroff.yml`, or per-host/group) wherever
IPMI-over-LAN has been disabled.

## What this does not cover

- **VM verification is best-effort only.** The network-reachability check
  confirms the guest OS stopped answering on a TCP port - it does **not**
  confirm the hypervisor actually powered the VM off (it could be paused,
  suspended, or simply firewalled). An authoritative check would need a
  vCenter/ESXi API call (e.g. via `govc` or `pyvmomi`), which this role
  deliberately does not add - ask if you want that wired in as a follow-up
  for the VMware home lab.
- **No power-on companion (yet).** The same iLO IP-detection/tool-selection
  scaffolding here could support a `system_poweron` role using
  `chassis power on` (ipmitool) or the equivalent Redfish action (ilorest) -
  not built, since it was not asked for; a natural, low-effort addition if
  wanted later.
- **`ilorest`'s password is visible on the delegate host's command line**
  for the duration of that task (no documented environment-variable
  equivalent to ipmitool's `-E`/`IPMI_PASSWORD`, as far as this role's
  author could confirm). Prefer the `ipmitool` path, or run this playbook
  from a locked-down jump host, if that matters in your environment.
- **Exact `ilorest` CLI syntax varies across major versions.** The
  `rawget /redfish/v1/Systems/1/` invocation here is the commonly documented
  form - verify it against `ilorest --help` / your installed version before
  relying on it in production, the same "best effort, verify against your
  install" spirit as the existing `ilorest serverinfo` call in
  `playbooks/collect_ilo.yml`.

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `system_poweroff_confirm` | `false` | **Required.** Must be `true` (pass via `-e`) or the role refuses to run. |
| `system_poweroff_allowed_distributions` | `[RedHat, OracleLinux]` | OS families this role will target. |
| `system_poweroff_allowed_major_versions` | `[7, 8, 9, 10]` | OS major versions this role will target. |
| `system_poweroff_skip_os_check` | `false` | Bypass the OS/version assert. |
| `system_poweroff_allow_control_node` | `false` | Allow targeting a host with `ansible_connection=local` (i.e. the control node itself). |
| `system_poweroff_message` | `Shutdown initiated by Ansible (system_poweroff role)` | Message passed to `shutdown -h now`. |
| `system_poweroff_skip_shutdown` | `false` | Skip the actual shutdown; run only the safety checks + verification. |
| `system_poweroff_verify_enabled` | `true` | Run the power-state verification step at all. |
| `system_poweroff_verify_delegate_host` | `localhost` | Host the BMC/network checks run FROM. |
| `system_poweroff_verify_timeout_seconds` | `300` | Overall timeout for verification. |
| `system_poweroff_verify_poll_interval_seconds` | `15` | Delay between BMC polling attempts. |
| `system_poweroff_bmc_tool` | `ipmitool` | `ipmitool` or `ilorest` - see comparison above. |
| `system_poweroff_bmc_user` / `system_poweroff_bmc_password` | `CHANGE_ME_*` placeholders | iLO admin credentials - vault these (see above). |
| `system_poweroff_bmc_address` | `""` (auto-detect) | Set as a per-host override to skip live in-band iLO IP detection. |
| `system_poweroff_network_check_port` | `22` | Port polled for the VM/non-BMC verification path. |

## Testing it

No standalone Python script here to unit test (unlike the
`idm_healthcheck_watch`/`idm_healthcheck_watcher` alerting roles) - this is
pure Ansible + shell against real hardware/BMCs, validated the same way
`playbooks/collect_ilo.yml` is:

```bash
# 1. Syntax only
ansible-playbook playbooks/system_poweroff.yml --syntax-check

# 2. Confirm exactly which hosts a pattern resolves to BEFORE adding
#    the confirm flag
ansible-playbook playbooks/system_poweroff.yml -e target_hosts=hpe_ilo_hosts --list-hosts

# 3. Dry run (Ansible --check mode) - the shutdown command itself is
#    still gated by system_poweroff_confirm, so this is a second,
#    independent safety net
ansible-playbook playbooks/system_poweroff.yml \
  -e target_hosts=client1.lab.example.com \
  -e system_poweroff_confirm=true \
  --check

# 4. Verify-only against a host you already powered off some other way
ansible-playbook playbooks/system_poweroff.yml \
  -e target_hosts=client1.lab.example.com \
  -e system_poweroff_confirm=true \
  -e system_poweroff_skip_shutdown=true

# 5. The real thing, one host first
ansible-playbook playbooks/system_poweroff.yml \
  -e target_hosts=client1.lab.example.com \
  -e system_poweroff_confirm=true

# 5b. Same as #5, but run interactively (no -e target_hosts) and let
#     it prompt you for the group/host instead:
ansible-playbook playbooks/system_poweroff.yml -e system_poweroff_confirm=true
#   Which inventory group or host(s) do you want to power off? (e.g.
#   hpe_ilo_hosts, or a comma-separated hostname list): client1.lab.example.com
```

Recommended first real test in the home lab: a single non-critical VM
(network-based verification path), then a single `[hpe_ilo_hosts]` member
with BMC access already confirmed working via
`ansible-playbook playbooks/collect_ilo.yml` (proves `ipmitool`/`ilorest`
already work against that host before you rely on them to confirm a
shutdown).

## Rollout

1. Vault real iLO credentials:
   `ansible-vault create group_vars/all/system_poweroff_secrets.yml`.
2. Un-comment `[hpe_ilo_hosts]` in your real `inventory.ini` and populate it
   with your DL380 Gen10/Gen11 hosts (same group `playbooks/collect_ilo.yml`
   already targets).
3. Run the "Testing it" sequence above against one host of each kind
   (physical, virtual) before trusting this against a larger batch.
4. `serial: 1` is the playbook default (one host at a time), processed in
   inventory-file order for the targeted group - override the batch size
   with `-e system_poweroff_serial=N` only once you are confident in the
   blast radius, and control the sequence by reordering hosts under that
   group in the inventory file.
