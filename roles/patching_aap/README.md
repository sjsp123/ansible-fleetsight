---
# patching_aap — controller-host patching with platform pre/post health checks.
#
# Adapted from an earlier internal build of the patching role and assembled
# against Red Hat's documented verification points for containerized AAP.
# Estate-specific machinery (vault credentials, estate hostnames) excluded.

## What it does

1. **PRE health gates** — zero failed systemd user units, free disk space on
   `/` and `/home` (installer preflight requirement), all 24 expected
   containers running (rootless podman, as the service account), platform
   gateway answering over HTTPS.
2. **Patch** — dnf metadata refresh, full package update.
3. **Reboot policy** — `needs-restarting` is reported, NOT acted on by
   default: a controller-host reboot would kill the job performing it.
   Reboots are manual events (set `patching_aap_reboot_enabled=true` to
   override deliberately).
4. **POST health gates** — the same container/unit/gateway checks
   re-asserted after the patch.
5. **History** — counts + full `dnf history info` transaction detail via
   the shared `patching_history` role.

## Sources for the check set

- Installer preflight / system requirements:
  https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/install-ref_cont_aap_system_requirements
- Post-install verification ("verify your installation", rootless podman,
  zero failed units, web UI):
  https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/pdfs/aap-install-2-7-pdf.pdf
- Maintain/upgrade-era health gates:
  https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/install-assembly_maintaining_containerized_aap

## Privilege note (important)

The playbook runs with `become: true`, but all `podman` and
`systemctl --user` tasks carry `become: false`: containerized AAP runs
rootless containers in the service account's podman store. Running
these checks as root queries a different, empty store and would
falsely report "no containers".
