# Incident: RHEL 10.3 (podman 6) breaks EE container networking — `invalid network "slirp4netns"`

**Date:** 10 Nov 2026 · **Affected:** the AAP controller host running containerized AAP
(rootless podman under the service account) · **Impact:** every AAP job failed instantly —
no task ever ran · **Status:** resolved same evening

## Symptom

After the scheduled patching to RHEL 10.3 and a reboot, every AAP job failed within seconds:

```
Error: invalid config provided: invalid network "slirp4netns"
```

No host ever reported a task failure — the execution-environment (EE) container itself
refused to start.

## Root cause — three layers stacked

1. **RHEL 10.3 AppStream ships podman 6.1.0** (up from 5.8.x). The [podman 6.0.0 release
   notes](https://github.com/podman-container-tools/podman/releases/tag/v6.0.0) state:
   *"Support for the slirp4netns rootless network stack has been removed. Please use Pasta
   instead."* The [RHEL 10 adoption notes](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/considerations_in_adopting_rhel_10/containers)
   had flagged slirp4netns as deprecated since 10.0.
2. **The controller launched every EE container with `--network slirp4netns:enable_ipv6=true`**
   via `DEFAULT_CONTAINER_RUN_OPTIONS` in the controller's `settings.py` — a setting AAP's own
   [troubleshooting documentation](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.4/html/troubleshooting_ansible_automation_platform/troubleshoot-networking)
   recommends for EE networking (historically, to move EEs off a conflicting default CIDR).
   After the podman upgrade, that option value became invalid.
3. **Discovery gotcha:** containerized AAP runs rootless under the service account (here:
   `svc_ansible`), so the config tree is `/home/<svcacct>/aap/...` — *not* `~/aap` (root's home)
   and not `/etc/containers/`. A config grep run as root with `~/aap/` silently misses it.

## Resolution

```python
# /home/<svcacct>/aap/controller/etc/settings.py  (backup first!)
DEFAULT_CONTAINER_RUN_OPTIONS = ["--network", "host",
                                 "--security-opt", "label=level:s0:c100,c200"]
```

Restart the controller services, rerun a job. EE containers now share the host's network
namespace; the controller can reach its own host trivially and outbound job traffic is
unchanged in practice.

## What was tried first, and why it failed (the learning value)

The two-step migration path *pasta → host* was chosen after pasta alternatives dead-ended:

1. **`slirp4netns:enable_ipv6=true` → `pasta`** in `settings.py` — pasta handles IPv6 natively
   (the `enable_ipv6` flag was slirp4netns-specific), so plain `pasta` was the minimal edit.
2. **Jobs then ran — but the controller could not SSH back to its own host.** Pasta's
   signature behaviour: *it copies the host's IP address into the container.* The EE therefore
   held the controller's own address (`192.168.187.x`), and any connection to it resolved as
   **local to the container** — where nothing listens on :22 → `Connection refused`. The
   pre-upgrade slirp4netns model (container on 10.0.2.x, NAT out) never had this problem.
3. **`pasta:--map-host-loopback=<host-addr>`** — the flag *sounds* like the fix (remap an
   address to the host), but it cannot intercept traffic to an address the **container
   considers its own** — routing short-circuits before pasta sees the packet. Refused again.
4. **`pasta:--no-copy-addrs`** — intended to stop the address copy; the container *still*
   ended up with the host's address, because **podman manages the address assignment itself**
   in pasta mode — pasta's own flags don't override podman's integration. Combined flags
   (`--no-copy-addrs,--map-host-loopback=...`) also failed for the same reason.

With the "correct" path empirically closed three ways, the host-network fallback is the
engineering answer for this environment.

## Trade-off, stated honestly

EE containers no longer have network isolation from the host — they can bind host ports and
reach everything the host reaches. In a single-tenant lab where the EEs already hold
fleet-wide credentials and their entire purpose is to reach the fleet, that's an acceptable
trade. If network isolation is ever needed again, the proper-shape alternative is:

- a dedicated podman network for the EEs (`podman network create`), and
- the controller host's `ansible_host` pointed at that network's gateway (which pasta maps
  to the host) — with the caveat that *every* connection into the controller host from its
  own EE context must use it.

## Verification

```bash
# pasta sanity (rootless context):
podman run --rm registry.access.redhat.com/ubi9/ubi-minimal echo pasta-works

# reproduce the original self-host gap (documents the pasta behaviour):
podman run --rm --network "pasta:--no-copy-addrs" registry.access.redhat.com/ubi9/ubi-minimal \
  bash -c 'timeout 3 bash -c "echo > /dev/tcp/<controller-ip>/22" && echo SELF-OK'
# → Connection refused: the copied address makes the host IP local to the container

# settings in place:
grep -A1 DEFAULT_CONTAINER_RUN_OPTIONS /home/<svcacct>/aap/controller/etc/settings.py

# end-to-end: rerun any job — tasks execute and hosts (incl. the controller itself) report
```

## Red Hat references

- [Introducing Apache Tomcat 10.1 in RHEL 10](https://developers.redhat.com/articles/2026/04/23/introducing-apache-tomcat-101-rhel-10)
  *(same AppStream-boundary pattern family — see the sibling incident in the IdM patching
  runbook: Dogtag PKI's Tomcat 9 → 10.1 swap)*
- [Podman 6.0.0 release notes](https://github.com/podman-container-tools/podman/releases/tag/v6.0.0)
  — the removal list: slirp4netns, CNI, iptables, cgroups v1
- [Considerations in adopting RHEL 10 — Containers](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/considerations_in_adopting_rhel_10/containers)
  — slirp4netns deprecation, pasta as the rootless default, `podman system migrate`
- [Communicating among containers (RHEL 10)](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/building_running_and_managing_containers/communicating-among-containers)
  — network modes, pasta vs slirp4netns differences (pasta copies host IPs; supports IPv6)
- [AAP troubleshooting — networking](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.4/html/troubleshooting_ansible_automation_platform/troubleshoot-networking)
  — the `DEFAULT_CONTAINER_RUN_OPTIONS` / `custom.py` mechanism this incident turned on

## Fleet-wide sweep (do once after any podman major bump)

```bash
ansible all -b -m shell -a \
  "grep -rs slirp4netns /etc/containers /home/*/.config /home/*/aap 2>/dev/null \
   | grep -v 'storage/overlay' | head -5 || true"
```

(Overlay-layer hits inside container image storage are harmless copies of collection code —
filter them out.)
