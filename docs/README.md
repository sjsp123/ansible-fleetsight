# Incident reports — operations documentation

Index of incident write-ups for the automation estate. Each report is
self-contained: symptom → root cause → resolution → lessons → Red Hat
references.

| Report | Date | Host | Impact |
|---|---|---|---|
| [RHEL 10.3 container networking — podman 6 / slirp4netns removal](incident-el10_3-container-networking.md) | 10–11 Nov 2026 | aap27 (AAP controller) | **All AAP jobs failed** — no task ran |
| RHEL 10.3 IdM/Tomcat 9→10.1 transition | 10 Nov 2026 | idm1 (IdM server) | Nightly IdM patch failed; KDC down after reboot |

---

# Today's report — 11 Nov 2026

## The podman issue (opened 10 Nov, closed 11 Nov)

**What happened.** After aap27's scheduled patch to **RHEL 10.3** and a manual reboot,
**every AAP job failed instantly** — job 429 (`hostinfo.yml`) died in ~10 seconds before
running a single task:

```
Error: invalid config provided: invalid network "slirp4netns"
```

No host ever reported a task failure — the execution-environment (EE) container itself
refused to start.

**Root cause — three layers stacked.**

1. **RHEL 10.3 AppStream shipped podman 6.1.0** (up from 5.8.x). Podman 6 **removed the
   slirp4netns rootless network stack entirely** — pasta is the replacement and the rootless
   default. Deprecated since RHEL 10.0, removed in 6.0.
2. **The AAP controller still referenced it.** `/home/svc_ansible/aap/controller/etc/settings.py`
   carried `DEFAULT_CONTAINER_RUN_OPTIONS = ["--network", "slirp4netns:enable_ipv6=true", ...]`
   — the setting that launches **every job's EE container**. The value was there legitimately:
   AAP's own troubleshooting documentation recommends it (historically, to move EEs off a
   conflicting default CIDR).
3. **Discovery gotcha.** Containerized AAP runs rootless under the service account
   (`svc_ansible`) — there is no `aap` user. The config tree is `/home/svc_ansible/aap/...`;
   a config grep run as root with `~/aap/` silently resolves to `/root/aap` and misses it.

**Resolution attempts — and why pasta fought back (the learning value).**

| Attempt | Result |
|---|---|
| `slirp4netns:enable_ipv6=true` → `pasta` in settings.py | Jobs ran again — **but** the controller could no longer SSH to its own host: `aap27` went unreachable in job output |
| Diagnosis of the self-reachability failure | **Pasta copies the host's IP address into the container.** The EE held `192.168.187.13` itself, so a connection to that IP resolved as *local* to the container — it landed inside the container, where nothing listens on :22 → `Connection refused`. Proven empirically: container → other hosts OK, container → own host refused. The pre-upgrade slirp4netns model (container on 10.0.2.x, NAT outbound) never had this problem |
| `pasta:--map-host-loopback=192.168.187.13` | No effect — the flag cannot intercept traffic to an address the **container considers its own**; routing short-circuits before pasta sees the packet |
| `pasta:--no-copy-addrs` (and combined with the remap) | **Did not take effect** — the container still held the host's address. Conclusion: **podman manages the address assignment itself in pasta mode**; pasta's own flags don't override podman's integration |

With the "correct" pasta path empirically closed three ways, the resolution was the
pragmatic one.

**Final state — network changes made (11 Nov).**

```python
# /home/svc_ansible/aap/controller/etc/settings.py
# (backup kept: settings.py.bak-20261110)
DEFAULT_CONTAINER_RUN_OPTIONS = ["--network", "host",
                                 "--security-opt", "label=level:s0:c100,c200"]
```

- EE containers now share the aap27 network namespace: the controller host is trivially
  reachable, outbound job traffic unchanged, and the same fix automatically covers the
  `patch-aap` job (which targets aap27 from itself).
- **End-to-end verification:** job 429 rerun green — all 7 hosts reported (including aap27),
  CSV report + timestamped history snapshot written.
- **Trade-off, stated honestly:** EEs no longer have network isolation from the aap27 host —
  they can bind host ports and reach everything the host reaches. In a single-tenant lab
  where EEs already hold fleet-wide credentials, that's an acceptable trade. If network
  isolation is ever required again: dedicated podman network for the EEs
  (`podman network create`) + the controller host's `ansible_host` pointed at that network's
  gateway.

**Fleet-wide sweep (11 Nov) — clean, two explainable findings:**

```bash
ansible all -b -m shell -a \
  "grep -rs slirp4netns /etc/containers /home/*/.config /home/*/aap 2>/dev/null \
   | grep -v 'storage/overlay' | head -5 || true"
```

- aap27: only the **dated backup file** (`settings.py.bak-20261110`) — the live config is
  fixed; the backup is the rollback artifact.
- aap1: the decommissioned AAP config tree still carries the old setting — harmless today
  (podman storage wiped, services disabled, linger off), but a landmine if aap1 is ever
  rebuilt as an AAP host; delete the tree as part of finishing that decommission.
- All other hosts: clean. **No time bombs elsewhere in the fleet.**

**Red Hat references**

- [Podman 6.0.0 release notes](https://github.com/podman-container-tools/podman/releases/tag/v6.0.0)
  — the removal list: slirp4netns, CNI, iptables, cgroups v1
- [Considerations in adopting RHEL 10 — Containers](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/considerations_in_adopting_rhel_10/containers)
  — slirp4netns deprecation, pasta as the rootless default
- [Communicating among containers (RHEL 10)](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/building_running_and_managing_containers/communicating-among-containers)
  — network modes; pasta copies host IPs (vs slirp4netns's defined address), supports IPv6
- [AAP troubleshooting — networking](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.4/html/troubleshooting_ansible_automation_platform/troubleshoot-networking)
  — the `DEFAULT_CONTAINER_RUN_OPTIONS` / `custom.py` mechanism this incident turned on
- [Introducing Apache Tomcat 10.1 in RHEL 10](https://developers.redhat.com/articles/2026/04/23/introducing-apache-tomcat-101-rhel-10)
  — the sibling boundary (IdM/Tomcat), same release, same pattern

**The pattern worth remembering.** Both 10 Nov incidents have the same shape — **RHEL 10.3
is a compatibility-boundary release**: it retired the `tomcat9` compat package (Dogtag PKI
moved to Tomcat 10.1) and removed slirp4netns (podman 6). Hosts relying on either shim
break at the boundary, and plain `dnf update` cannot cross it. After any minor-version
jump: sweep for removed-stack references *before* trusting the next scheduled run (the
sweep command above), and expect the fix to be a documented **swap/migration**, not a flag.

## Background — what is pasta, and why RHEL switched

**pasta** is the namespace-side counterpart of **passt** ("**P**lug **A** **S**imple
**S**ocket **T**ransport") — a user-mode networking daemon written from scratch by the same
ecosystem that maintained `slirp4netns`. Two programs, one design:

- **passt** — connects a *virtual machine* to the network: a full TCP/IP, UDP and ICMP stack
  implemented **in user space**, bridging a VM's interface to plain host sockets. No bridge,
  no root, no host-side TUN.
- **pasta** — the same engine for *containers/namespaces*: it "patches" a network namespace
  into the host's network, and is what podman now uses for **all rootless container
  networking**.

The architectural idea: instead of kernel networking primitives that need privilege
(bridges, routed veth pairs, NAT rules), the container gets a **tap interface connected to
the pasta process over sockets**, and pasta speaks real TCP/IP on the host's behalf with
ordinary unprivileged sockets — a deliberately tiny code base with no dynamic allocation
after startup and heavy seccomp filtering.

**How it differs from slirp4netns** — slirp4netns was 1990s QEMU `libslirp` code adapted
for containers: a NAT box giving containers a private `10.0.2.x` space, hidden behind the
host. pasta throws that model away (per the
[RHEL 10 networking docs](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/building_running_and_managing_containers/communicating-among-containers)):

| | slirp4netns | pasta |
|---|---|---|
| Address model | Fixed private `10.0.2.15`, NAT'd out | **Copies the host's IP address and routes** into the container |
| Outbound appearance | Source-NAT'd, origin hidden | Traffic appears to come **from the host's own IP** — the container shares the host's network identity |
| IPv6 | Weak/awkward | **First-class** — RA/SLAAC handled, IPv6 port forwarding |
| Port forwarding | Complex, loses source IP | Preserves the **original client source IP** |
| Performance | The long-standing complaint | Substantially faster, designed around efficient socket handling |
| Security posture | Old code base, larger surface | Written for confidential computing: minimal, sandboxed, hardened |

Row one is the one that bit this lab: **because pasta copies the host's IP, a rootless
container literally holds the host's address** — so a connection from the container to "the
host's IP" is, from the container's routing perspective, a connection to *itself*. That is
why the EE containers could not SSH back to their own controller after the migration, and
why no pasta flag could remap it: the container's kernel resolves that address locally
before pasta ever sees the packet.

**Why Red Hat switched — the timeline was staged, not sudden:**

1. **RHEL 9.2** (podman 4.4.1) — pasta appears as an *option*
2. **RHEL 9.5** — pasta becomes the **default** for rootless containers
3. **RHEL 10.0** — slirp4netns formally **deprecated** ("removed in a future major release")
4. **RHEL 10.3** (podman 6.1) — slirp4netns support **removed**; pasta is the only path

The reasons behind it:

- **slirp4netns is end-of-life upstream** — its maintainer lineage moved to passt; shipping
  a dying user-space stack conflicts with Red Hat's maintenance obligations (identical
  pattern to the `tomcat9` compat package: a deprecation clock, with 10.3 as the deadline)
- **Performance** — rootless container networking was a known bottleneck; pasta fixes
  throughput and latency without root
- **Security / Confidential Computing** — passt is the network backend for confidential VM
  workloads (Kata et al.): minimal, seccomp-sandboxed, allocation-free — one hardened stack
  serving containers *and* confidential VMs instead of maintaining two
- **Correctness** — IPv6, source-IP-preserving forwarding, and the shared-host-identity
  model solve limitations slirp4netns could never fix architecturally

**The cost of the new model** is the address-copying edge: containers present themselves as
the host on the network (cleaner for source-IP-sensitive services), but lose the fixed
"gateway = host" escape hatch slirp4netns provided — which is what forced the
`--network host` resolution for the AAP EEs above.

**Summary:** *"RHEL 10.3 replaced slirp4netns with pasta — a
from-scratch, seccomp-hardened user-mode network stack that gives rootless containers the
host's own network identity instead of NAT — faster, IPv6-correct, source-IP-preserving,
and shared with the confidential-computing stack. The migration cost us: the copied-IP
model breaks container-to-own-host connections, which slirp4netns's NAT model used to
allow."*
