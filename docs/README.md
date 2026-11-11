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
