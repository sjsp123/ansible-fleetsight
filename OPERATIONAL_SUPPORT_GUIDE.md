# Operational Support Guide — Log Search, Insights, LinuxDB & Reports

## Version History

| Date | Change | Notes |
|---|---|---|
| 2026-09-12 | Initial version of this guide. | Covers Log Search UI (Search + Insights) and LinuxDB Dashboard (fleet overview, per-host tabs, IdM Health), plus self-serve troubleshooting steps. |
| 2026-09-14 | Added "Checking who's logged in" under Quick reference. | Documents that `journalctl -u log-search-ui` / `linuxdb-dashboard` now includes the Basic-Auth username on every request line, and the Local-mode vs Kerberos-mode caveat on what that username actually tells you. |

---

## Contents

- [Version History](#version-history)
- [About this guide](#about-this-guide)
- [Quick reference](#quick-reference)
    - [Logging in](#logging-in)
    - [Checking who's logged in](#checking-whos-logged-in)
- [Log Search UI](#log-search-ui)
    - [Search page](#search-page)
    - [Insights page](#insights-page)
    - [Log Search — troubleshooting](#log-search-troubleshooting)
- [LinuxDB Dashboard](#linuxdb-dashboard)
    - [Fleet overview](#fleet-overview)
    - [Per-host drill-down](#per-host-drill-down)
    - [Data freshness](#data-freshness)
    - [IdM Health](#idm-health)
    - [Package Search](#package-search)
    - [LinuxDB — troubleshooting](#linuxdb-troubleshooting)
- [Monitoring & Alerting](#monitoring-alerting)
- [Control node scripts (on-demand / manual runs)](#control-node-scripts-on-demand-manual-runs)
    - [Fleet data refresh](#fleet-data-refresh)
    - [One-off diagnostic checks (ad hoc — not saved to the dashboard)](#one-off-diagnostic-checks-ad-hoc-not-saved-to-the-dashboard)
    - [Additional ad hoc reports](#additional-ad-hoc-reports)
    - [Redeploying the web dashboards after a code change](#redeploying-the-web-dashboards-after-a-code-change)
    - [Patching](#patching)
    - [Time synchronization (chrony via Red Hat System Roles)](#time-synchronization-chrony-via-red-hat-system-roles)
    - [Powering off hosts](#powering-off-hosts)
- [When something looks broken](#when-something-looks-broken)

---

## About this guide

This is the day-to-day "how do I use this" guide for the web dashboards
described in this document — **Log Search UI**, its **Insights** page,
the **LinuxDB Dashboard** (fleet overview, per-host reports, and IdM
Health), and the background **monitoring/alerting** mechanisms that
email you when something needs attention. It's written for anyone on
the team who needs to use these tools or troubleshoot a problem while
the person who usually maintains them is unavailable — you shouldn't
need anything beyond a browser, and for a handful of checks, SSH access
to the relevant server.

**If something breaks and you're reading this because of it:** jump
straight to [When something looks broken](#when-something-looks-broken)
— every troubleshooting table in this guide gives concrete steps to
try yourself first.

**Keeping this guide current:** every time one of these tools changes
in a way that affects how the team uses it (new feature, new tab, new
troubleshooting step, a bug that changed expected behaviour), add a row
to the [Version History](#version-history) table above — that's how
the team can tell, at a glance, whether anything they rely on has
changed since they last read this.

---

## Quick reference

| Tool | What it's for | Default URL | Default port | Login |
|---|---|---|---|---|
| Log Search UI | Search centralized syslog/journald logs across the whole fleet | `https://<admin1>:8081/` | `8081` | Yes — see [Logging in](#logging-in) |
| Log Search — Insights | Alert-volume trends, severity breakdown, noisy-pattern detection over the same log data | `https://<admin1>:8081/insights` | `8081` | Same login as Log Search UI |
| LinuxDB Dashboard | Fleet-wide inventory + per-host diagnostic drill-down (disk, processes, health, hardware, config drift, IdM health) | `https://<admin1>:8082/` | `8082` | Yes — see [Logging in](#logging-in) |
| Monitoring & alerting | Background checks that email you the moment something needs attention, instead of waiting for someone to open a dashboard | n/a — arrives by email | n/a | n/a |
| This guide (live copy) | This same document, always up to date, viewable from any browser — no SSH or repo access needed | `https://<admin1>:8082/guide` | `8082` | Same login as LinuxDB Dashboard |

`<admin1>` is the control node's hostname/FQDN — check your browser
bookmarks or history, or a teammate who has used these pages before,
if you don't already know it. Both apps use a self-signed TLS
certificate by default in the lab (browsers will warn "not secure" /
"not private" — that's expected, click through) unless a proper
corporate-signed certificate has since been installed.

### Logging in

Both dashboards sit behind a login before showing any data:

- **Local mode** (the lab default): a single shared username/password.
  This is a shared secret and deliberately isn't printed in this guide
  — check wherever your team keeps shared credentials (e.g. a password
  manager or vault). To confirm which mode a given dashboard is
  actually running in without needing that secret, an admin on the
  host itself can check the deployed config file
  (`/opt/log_search_ui/config.py` or `/opt/linuxdb_dashboard/config.py`)
  for the `AUTH_MODE` value — it's plain text, not a credential.
- **Kerberos/AD/IdM mode** (enabled via `dashboard_auth_mode: kerberos`): log in with your
  own existing domain username and password — same credentials you use
  elsewhere on the domain, nothing new to remember. If your login is
  rejected in this mode, it usually means either your account isn't in
  the required group for dashboard access, or there's a KDC/Kerberos
  reachability problem on the dashboard host itself — see
  [When something looks broken](#when-something-looks-broken).

### Checking who's logged in

Every request to either dashboard is recorded in that service's own
journal, including the username that was presented at login:

```bash
journalctl -u log-search-ui -f      # Log Search UI + Insights
journalctl -u linuxdb-dashboard -f  # LinuxDB Dashboard + this guide
```

Each line shows the source IP, the username, the timestamp, the
request path, and the HTTP status (`200` = succeeded, `401` =
rejected):

```
10.20.30.41 - jsmith [14/Sep/2026 09:12:03] "GET /insights HTTP/1.1" 200 4213
```

**In Local mode** (the lab default — see [Logging in](#logging-in)
above), this only tells you *which username string was typed in*, not
*which real person* that was — everyone shares the same single
username/password pair, so the log can't tell team members apart.
It's still useful for confirming that someone logged in, from where,
and when, and for spotting a run of `401`s from the same IP (repeated
failed attempts).

**In Kerberos/AD/IdM mode**, the same log line reflects each person's
own domain username, so it becomes a genuine per-person access record
for that app. For the authoritative record of the underlying
authentication itself (not just what this app chose to log), the
domain controller/IdM server's own logs are the source of truth — ask
whoever administers your realm.

---

## Log Search UI

**What it's for:** searching the centralized log database fed by
rsyslog + journald across the fleet, instead of SSHing into each box
and grepping flat files one at a time.

### Search page

A single form: message pattern, host, syslog tag, severity, and a
since/until time window.

- **Contains** mode — plain, case-insensitive substring search (fast,
  the default; good for "does this word appear anywhere").
- **Regex** mode — POSIX case-insensitive regex, for anyone used to
  `grep -E` pattern searches (e.g. `Failed password.*root` to match
  either order isn't valid regex, but `sshd.*Fail(ed|ure)` is — same
  regex rules as `grep -E`).
- Results are paginated (500/1000/2000/5000/"All" per page) — Previous
  and Next preserve whatever filters you already set, they don't reset
  the search.
- Every result table row shows enough context (timestamp, host, tag,
  severity, message) to decide if it's what you're looking for without
  drilling in further.

**Common tasks:**

- *"Did anyone fail to SSH into `<host>` recently?"* — Host = `<host>`,
  Message = `Failed password`, since = last 24h.
- *"Show me everything from `sshd` on the whole fleet in the last
  hour"* — Tag = `sshd`, since = last 1h, leave Host blank.
- *"I only remember part of the message and it might have different
  wording each time"* — switch to Regex mode and use `.*` for the
  parts you're unsure of.

### Insights page

A heuristics dashboard over a selectable time window (1h/24h/7d/30d):

- **Headline counters** — SSH auth failures, sudo commands run, total
  password failures, distinct source IPs behind failed SSH attempts.
- **Alert volume over time** — a stacked, severity-coloured bar chart.
  A bar that's mostly blue/grey is routine Informational/Debug noise;
  a bar with a red/orange cap is where something more severe happened
  in that window — hover any segment for the exact count.
- **Severity breakdown** — a donut chart + table; click a row to jump
  straight to those raw log lines on the Search page.
- **Top repeated / similar alerts** — clusters the noisiest recurring
  log lines and flags each cluster **Likely noise** or **Review /
  Genuine?** — this is the fastest way to spot a real, recurring
  problem (e.g. a service repeatedly throwing a permission error)
  without reading thousands of individual log lines. Start here if
  you're not sure what to search for.
- **CSV exports** — a management summary and a full ops-detail report,
  for sharing outside the dashboard (email, ticket attachment, etc.).
- **Suppressing a known-noisy pattern** — if something you already know
  about keeps cluttering Top repeated alerts, click **Dismiss** right
  on that row (or **Dismiss all** to suppress every occurrence of that
  pattern for that service). This takes effect immediately — no config
  change, redeploy, or service restart needed. To review or reverse a
  suppression later, open the **Suppressed patterns** section further
  down the Insights page and use **Un-suppress**.

### Log Search — troubleshooting

| Symptom | Steps to try |
|---|---|
| Page won't load at all | Try the URL again after a minute (the service may be mid-restart). If you have SSH access to the collector host: check `systemctl status log-search-ui`; if it's stopped, `sudo systemctl restart log-search-ui`, then `journalctl -u log-search-ui -n 100 --no-pager` for the reason it stopped (commonly a bad TLS certificate or a Python traceback — the last few lines usually name the exact cause). |
| Login rejected | Confirm you're using the right credential type (local shared password vs. your own domain login — see [Logging in](#logging-in)). For Kerberos mode specifically, run `kinit <your-username>` on the dashboard host to test whether the domain/KDC accepts your password independent of the dashboard — if `kinit` itself fails, the problem is on the domain side, not the dashboard. |
| Search runs but returns nothing, even for something you know happened | Double-check your time window and filters aren't too narrow (wrong host spelling, or "since" set later than the event). If filters look right, check the log database itself: `sudo systemctl status postgresql` on the log collector host — if it's stopped, `sudo systemctl restart postgresql` and retry the search a minute later. |
| A 500 error / "Internal Server Error" appears | Check `journalctl -u log-search-ui -n 100 --no-pager` for the Python traceback (systemd captures it directly) — the last few lines usually name the exact cause (e.g. database connection refused, disk full). `sudo systemctl restart log-search-ui` clears most transient cases. |

---

## LinuxDB Dashboard

**What it's for:** a read-only view of the whole fleet's inventory and
per-host diagnostics — patch status, disk usage, running processes,
service health, hardware facts, and config-file drift — without
logging into every server by hand.

### Fleet overview

One table, one page: hostname/FQDN/IP, OS version, patch status
(`DNF_STATUS`), whether a reboot is required, Satellite membership,
last boot time and uptime, failed-service count, NTP sync state, and
SELinux enforcement mode.

- **Jump to a host by name** — type a hostname into the search box
  above the table and press Enter (or click **View reports**) to go
  straight to that host's detail tabs, instead of scrolling the table
  to find and click its link. Start typing and known hostnames appear
  as suggestions; an unrecognised hostname shows a message telling you
  it isn't valid rather than silently failing.
- **Click any column heading to sort by it** — click again to reverse
  the order. The column currently sorted shows a bold, coloured
  heading with an underline and a solid arrow icon, so you can always
  tell which sort is active at a glance; every other heading shows a
  faint double-arrow to indicate it's clickable too.
- **History selector** — step back to a previous captured run instead
  of only ever seeing "latest live" data. Useful for before/after
  comparisons around a patch window.

### Per-host drill-down

Click any hostname to open tabbed detail for that host, all sourced
from the same timestamped collection run (the run's timestamp is shown
and independently selectable per tab):

| Tab | What's in it |
|---|---|
| **Overview** | Identity/baseline facts: OS/kernel, patch & reboot status, Satellite membership, uptime, failed-service count, NTP/chrony sync state, SELinux mode, CPU/memory/swap sizing, hypervisor info, IPA realm |
| **Disk & Filesystem** | Every mounted filesystem (type/size/used/use%/mountpoint), a filesystem-utilisation warning check, and an fstab-vs-mounted validation pass — the fast way to spot a filesystem nearing capacity, or an fstab entry that silently didn't mount |
| **Processes** | Full process table (host/user/PID/command) plus a summary: process/user counts and top-CPU / top-memory consumers — the first stop when a host is reported "slow" |
| **Server Health** | Load average, memory/swap, CPU iowait, inode usage, failed systemd services, key service state (`sshd`/`firewalld`/`chronyd`/`auditd`/`crond`), OOM/kernel errors, D-state processes, network errors, logged-on users, failed SSH/sudo attempts, locked accounts, SSSD/IdM/KRB5/DNS reachability, and certificate-expiry tracking for every IPA/PKI cert on the host |
| **System & Hardware Info** | BIOS/baseboard/chassis facts, CPU model/socket/core/thread counts, memory slot population, private and public IPs |
| **Installed Packages** | Every RPM package installed on the host — name, version, release, architecture and install date. See also [Package Search](#package-search) below to search across every host at once. |
| **Config Files** | Tracked security/ops config files (`sshd_config`, `sudoers`, `firewalld.conf`, `krb5.conf`, `sssd.conf`, audit rules, PAM stacks, etc.), each flagged **Unchanged** or **Missing** against the last captured baseline, with a link to the actual captured content |
| **IdM Health** *(only shown for IdM-related hosts that have data)* | Latest `ipa-healthcheck` results for that host — see [IdM Health](#idm-health) below |

### Data freshness

Fleet inventory and per-host reports normally refresh automatically
**four times a day** (every 6 hours) via a scheduled job on the control
node — you don't need to do anything to keep this current. If a page
shows "No hostinfo.csv found" or "No data yet for this host":

- Check disk space on the data mount: `df -h /linuxdb` on the control
  node — a full filesystem is the most common reason a scheduled
  collection silently fails to write its output.
- Check when the data actually last updated: `ls -la /linuxdb/hostinfo`
  (and the equivalent path for the report you're looking at) shows the
  last-modified time of the most recent run.
- If you have access to the control node as the automation service
  account, the scheduled job's own output is logged under
  `~/ansible/logs/cron_hostinfo.log` and `~/ansible/logs/cron_dashboard.log`
  — `tail -50` either file to see whether the last scheduled run
  actually succeeded and when it last ran.

### IdM Health

The IdM Health tab shows a point-in-time snapshot from the latest
`ipa-healthcheck` run against that host, refreshed on the same schedule
as everything else in LinuxDB. For the separate, always-on email alert
that fires the moment a real IdM problem is found (rather than waiting
for you to open this tab), see
[Monitoring & Alerting](#monitoring-alerting) below.

### Package Search

A **Package Search** tab in the header (shown on every page) lets you
search installed packages across the whole fleet in one go, instead of
opening each host's Installed Packages tab in turn. Type a package
name — e.g. "firefox" — and it lists every host that currently has it
installed, with the exact version, release and architecture on each;
click any hostname in the results to jump straight to that host's own
Installed Packages tab. Click a column heading to sort the results by
it, same as the fleet overview table.
Results reflect whatever was captured on each host's last refresh (see
[Data freshness](#data-freshness)) — a package installed since then
won't show up until the next scheduled or manual refresh.

### LinuxDB — troubleshooting

| Symptom | Steps to try |
|---|---|
| Page won't load at all | Try again after a minute (the service may be mid-restart). If you have SSH access: check `systemctl status linuxdb-dashboard`; if it's stopped, `sudo systemctl restart linuxdb-dashboard`, then `journalctl -u linuxdb-dashboard -n 100 --no-pager` for the reason it stopped. |
| Login rejected | Same checks as Log Search UI — see [Logging in](#logging-in) and the `kinit` test above for Kerberos mode. |
| A host is missing from the fleet table entirely | Check `/linuxdb/hostinfo` for that host's row and its timestamp (see [Data freshness](#data-freshness)); on the control node, `cat ~/ansible/inventory` confirms whether the host is actually listed in the automation inventory at all. |
| A tab shows "No data yet for this host" | Normal for a brand-new host until the next scheduled cycle — see [Data freshness](#data-freshness) for how to confirm the last run's timing. |
| Table won't sort / clicking a heading does nothing | Confirm you're clicking directly on the column heading text/icon, not the row below it; try a hard refresh (Ctrl/Cmd+Shift+R) in case an old cached page is loaded. If it still doesn't respond, open your browser's developer console (F12) and check for a JavaScript error — that points to a front-end bug rather than missing data. |

---

## Monitoring & Alerting

Beyond the two dashboards above, several small, independent background
mechanisms email you directly the moment something needs attention,
instead of waiting for someone to open a dashboard or for the next
scheduled report. All of them deliver mail through the same local mail
relay already configured on the collector host — nothing to log into,
they just land in your inbox.

**The common pattern:** most of these are a **check** + **watcher**
pair. The *check* half runs locally on every monitored host on a short
systemd timer and, when it finds a problem, writes one fixed-wording
line to syslog — it never sends mail itself and needs no mail relay of
its own. The existing log-forwarding pipeline carries that line to the
collector like any other log message. The *watcher* half runs only on
the collector, polls the log database for those specific messages, and
sends the actual alert email (batched, not one email per line). Two
mechanisms below don't follow this pattern — `log_alerter` reacts to
any log line at or above a severity threshold rather than a specific
fixed message, and `host_availability_watcher` checks hosts directly
over the network rather than waiting for them to log anything at all.

| Mechanism | What it watches | How often | What triggers an email |
|---|---|---|---|
| **Service failure** (`service_failure_watcher`) | Any systemd unit failing, on any log-forwarding host | Collector polls every 30s | Any unit entering the `failed` state |
| **Log alerter** (`log_alerter`) | Every log line landing in the log database | Collector polls every 60s | Any line at or above a configured severity threshold |
| **Host availability** (`host_availability_watcher`) | Whether each host responds to ping and its SSH port | Collector checks directly every 60s | A host's state changes: reachable → unreachable, reachable → SSH-only-unreachable, or back to reachable |
| **Disk space** (`disk_space_check` + `disk_space_watcher`) | Real filesystem disk-space and inode usage on every host | Per-host check every 5 min; collector polls every 60s | Usage crosses 85% (WARNING) or 95% (CRITICAL) |
| **Chrony/NTP** (`chrony_check` + `chrony_watcher`) | NTP sync state, chronyd running, chrony leap status on every host | Per-host check every 15 min; collector polls every 180s | NTP not synchronized, chronyd not active, or leap status abnormal |
| **IdM health** (`idm_healthcheck_watch` + `idm_healthcheck_watcher`) | `ipa-healthcheck` findings on IdM hosts | Per-host check every 30 min; collector polls every 60s | Any non-`SUCCESS` finding, or the check itself failing to run |

**What to do when you get one of these emails:**

- **Service failure** — SSH to the named host and check
  `systemctl status <unit>` / `journalctl -u <unit> -n 100 --no-pager`,
  or look at that host's **Server Health** tab in LinuxDB for its
  failed-service count.
- **Log alerter** — the email includes the matching log lines; open
  Log Search UI and search for the same host/text for full context
  around that time.
- **Host availability** — the named host isn't responding to ping or
  SSH at all. Try reaching it yourself (ping, SSH, or your
  hypervisor/console access) before assuming the network path from the
  collector itself is the problem.
- **Disk space** — open that host's **Disk & Filesystem** tab in
  LinuxDB to see every mounted filesystem's current usage, or check
  directly with `df -h` / `df -i` over SSH.
- **Chrony/NTP** — check that host's **Server Health** tab (NTP sync
  state is listed there) or run `timedatectl` / `chronyc tracking`
  directly over SSH.
- **IdM health** — check the **IdM Health** tab for that host in
  LinuxDB, or run `ipa-healthcheck --output-type=json` directly on the
  affected host for the full finding detail.

**A note on an older mechanism:** you may occasionally see references
to a per-host service-failure alert that installed itself directly
onto every monitored unit. That approach has been retired in favour of
`service_failure_watcher` above, which covers the same need centrally
from the collector without needing anything installed on individual
hosts — if you run across it, it's legacy and not the active
mechanism.

**If you expect an alert email and it never arrives:**

- On the collector, check the relevant watcher's timer is actually
  running: `systemctl status <watcher-name>.timer` (e.g.
  `disk-space-watcher.timer`, `chrony-watcher.timer`).
- Check the collector's local mail queue: `mailq` — a backlog there
  points to a mail-relay delivery problem rather than the watcher
  itself.
- Check the watcher's own log for errors reaching the log database:
  `journalctl -u <watcher-name> -n 100 --no-pager`.

---

## Control node scripts (on-demand / manual runs)

The scheduled collection described in [Data freshness](#data-freshness)
keeps everything current automatically, four times a day. The scripts
below exist for when you need something sooner — an immediate recheck
of one host, a full manual refresh outside the normal schedule,
redeploying a dashboard after a code change, or patching a host. They
live on the control node in the automation service account's home
directory and need SSH access to that account to run — most of the
team won't need these day to day, but they're documented here so
anyone covering can use one without having to reverse-engineer what it
does first.

### Fleet data refresh

| Script | What it does | Notes |
|---|---|---|
| `ansible_hostinfo.sh` | Refreshes `hostinfo.csv` — the fleet Overview table, and each host's Overview tab (OS/kernel, patch status, uptime, etc.). | Interactive: asks which host or inventory group to refresh. Press Enter alone to refresh the whole fleet (the default). |
| `ansible_linuxdb.sh` | The full manual refresh: refreshes `hostinfo.csv` for the whole fleet, re-collects every per-host diagnostic report (Disk & Filesystem, Processes, Server Health, System & Hardware Info), re-captures the tracked config files, and redeploys the LinuxDB Dashboard service itself. | Not interactive — always targets the whole fleet. Briefly restarts the LinuxDB Dashboard service while it redeploys, so expect a few seconds of "page won't load" on that dashboard only. |

### One-off diagnostic checks (ad hoc — not saved to the dashboard)

These five push one of the same check scripts the scheduled collection
normally runs out to a host (or group) of your choosing, and print its
output straight to your screen there and then. They're useful for an
immediate answer without waiting for the next scheduled run — but the
result only ever appears in your terminal; it is **not** saved into
the dashboard's history, so the matching tab won't reflect it.

| Script | Same check as tab | What it looks at |
|---|---|---|
| `ansible_df_check.sh` | Disk & Filesystem | Per-mount filesystem usage (WARNING at 85%, CRITICAL at 95%), and whether currently mounted filesystems match `/etc/fstab`. |
| `ansible_proc_check.sh` | Processes | Every non-system-account process running on the host (user, PID, command). |
| `ansible_svr_check.sh` | Server Health | Load average, memory/swap, CPU/iowait, inode usage, failed systemd services, key service state, OOM/kernel errors, logged-in users, failed SSH/sudo attempts, IdM/DNS reachability, and certificate expiry. |
| `ansible_system_stats.sh` | System & Hardware Info | BIOS/baseboard/chassis facts, CPU topology, memory slot population, and network device/IP info. |
| `ansible_rpm_report.sh` | Installed Packages | Every installed package on the host, with its version, release, architecture and install date. |

All five are interactive and always ask for a target host or inventory
group by name — there's no default, so have the hostname (or group)
ready before you start.

### Additional ad hoc reports

A few more read-only reports that don't have a matching dashboard tab
— each writes its result to a file on the control node (or straight to
your screen) rather than into LinuxDB or Log Search.

| Script | What it reports | Where the output goes |
|---|---|---|
| `ansible_ilo_inventory.sh` | HPE firmware/BIOS/iLO version and storage-controller info for every physical host in the `hpe_ilo_hosts` group. | CSV at `/var/tmp/hpe_firmware_inventory.csv` on the control node. Not interactive — always runs against the whole `hpe_ilo_hosts` group. |
| `ansible_dnf_history.sh` | Each targeted host's most recent patch transaction — packages installed/upgraded/removed and when it ran. | CSV at `/tmp/dnf_history_report.csv` on the control node. Interactive: asks for a target host/group (defaults to the whole fleet if you just press Enter). |
| `ansible_critical_alerts.sh` | A 30-day scan per host for critical/kernel issues, failed services, authentication failures, disk/filesystem errors, and recent reboots. | Per-host report plus one consolidated report under `/var/tmp/server-findings/` on the control node. Interactive: asks for a target host/group (defaults to the whole fleet if you just press Enter). |
| `ansible_idm_healthcheck.sh` | The same `ipa-healthcheck` findings behind the [IdM Health](#idm-health) tab, run on demand — useful right after patching or rebooting an IdM host rather than waiting for the next scheduled check. | Printed to your screen only. Interactive: asks for a target host/group — type the actual IdM host/group rather than accepting the fleet-wide default, since this only makes sense against IdM servers. |
| `ansible_sudo_audit.sh` | A fleet-wide audit of every `sudoers`/`sudoers.d` rule, flagging root-equivalent and NOPASSWD grants for review. | CSV at `/var/tmp/sudo_audit_report.csv` on the control node, restricted to owner-read-only — it lists privileged access per host, so treat it accordingly. Not interactive — always fleet-wide. |
| `ansible_system_health_report.sh` | Uptime, load average, network totals, and the status of key services (`sshd`, `firewalld`, `multipathd`) for every host in the fleet. | Printed to your screen, plus a copy per host under `playbooks/reports/` on the control node. Not interactive — always fleet-wide. |
| `ansible_lineage.sh` | Traces a process, port, open file, or container on a chosen host back to the systemd unit, cron job, SSH session, or shell actually responsible for it. | Printed to your screen only. Fully interactive — asks for the target host, then whichever of process/PID/port/file/container you want to check (press Enter to skip any that don't apply). |

As with the checks above, none of these results are saved into the
dashboards — each run only produces its file (or screen output) at the
time you run it.

### Redeploying the web dashboards after a code change

Only relevant if a dashboard's code has just been changed — these pull
in the newest version and restart the affected service. Running one
against unchanged code is harmless (it just restarts the service), but
there's no reason to run them otherwise.

| Script | What it redeploys |
|---|---|
| `ansible_pushdb_change.sh` | LinuxDB Dashboard only. |
| `ansible_push_logsearch_change.sh` | Log Search UI (Search + Insights) only. |
| `ansible_push_all_changes.sh` | Both of the above, one after the other — use this when a change touched both apps (e.g. a shared header/navigation update). Stops at the first failure rather than pushing the second app on top of a broken first one. |

Each briefly restarts the corresponding service — expect a few seconds
of "page won't load" on that dashboard while it happens.

### Patching

| Script | What it does | Notes |
|---|---|---|
| `ansible_update_system.sh` | Patches a host or inventory group and reboots automatically afterwards if the patch requires it. | Interactive: asks for the target host/group, then whether to auto-reboot (defaults to yes if you just press Enter). Hosts are patched in batches (30% of the target group at a time by default), not all at once, so a group patch doesn't take everything down together. |

### Time synchronization (chrony via Red Hat System Roles)

| Playbook | What it does | Notes |
|---|---|---|
| `ansible-playbook playbooks/deploy_timesync_role.yml` (run from the control node) | Deploys/updates the chrony (NTP) configuration on the target host(s) via Red Hat's `redhat.rhel_system_roles.timesync` role and restarts `chronyd` if it changed. | Interactive: asks for the target host/group, with no default — you must type it explicitly. Idempotent and safe to re-run: `chronyd` is only restarted when the configuration actually changed, so expect a short gap in NTP sync only in that case. |

This is the only supported way to (re)deploy chrony configuration —
the repo's previous own-template deployment scripts were removed.
What you need to know as an operator:

| Question | Answer |
|---|---|
| NTP servers | The effective `chrony_ntp_servers` YAML list for each host: `group_vars/all/site_hosts.yml` supplies the default; `group_vars/<realm_group>/site_hosts.yml` can override it per realm. Do not use comma-separated INI values. The home lab overrides this in `group_vars/homelab.yml`: clients sync from `idm1` (which serves NTP to the lab subnets — the playbook adds the `allow` directives), with the public pool as fallback; `idm1` itself tracks the pool directly. |
| Does monitoring change? | **No.** `chrony_check` / `chrony_watcher` alerting checks whatever chrony configuration is actually running. |
| Collection dependency | `redhat.rhel_system_roles` — not on public Galaxy; it ships as the AppStream `rhel-system-roles` RPM (`sudo dnf install rhel-system-roles` on the control node). |

Details, caveats, and the offline collection install steps live in
[`rhel_system_roles/README.md`](rhel_system_roles/README.md).

### Powering off hosts

**Use these deliberately, not casually — both genuinely power hosts
off.** Two scripts exist; pick based on how strong a guarantee you
need that a host is actually off, not just unreachable over the
network:

| Script | Guarantee | Use when |
|---|---|---|
| `ansible_system_poweroff.sh` | Hardware-confirmed for hosts in the `hpe_ilo_hosts` group — checks the physical iLO/IPMI management interface directly, independent of the OS. Falls back to a network check for everything else. | You have iLO/BMC access in place and need to be certain a physical host is actually powered down (e.g. before physically removing a drive). |
| `ansible_poweroff_all.sh` | Network reachability only, for every host, physical or virtual — proves the host stopped answering on a port, not that the hardware itself is off. | A same-day power-down where there's no time to validate the iLO/IPMI path, or the target group is entirely virtual. |

Both scripts follow the same safe sequence:

1. Ask for the target host or inventory group.
2. Print the exact host list that would be affected, before touching
   anything.
3. Require you to type `YES` in full capitals to continue — pressing
   Enter, or typing anything else, aborts with nothing powered off.
4. Shut hosts down one at a time by default, in inventory order, so a
   clustered/replicated service never loses more than one member at
   once.

Review the printed host list carefully before typing `YES` — that is
the only safety check standing between you and the hosts actually
shutting down.

---

## When something looks broken

A quick decision guide, in order:

1. **Can you reach the page at all?**
   - No → the underlying service may be down or the host may be
     unreachable. If you have SSH access to the collector host, check
     `systemctl status log-search-ui` / `linuxdb-dashboard` and
     `sudo systemctl restart <service>`, then
     `journalctl -u <service> -n 100 --no-pager` for the reason it
     stopped.
2. **Page loads, but login fails?**
   - Confirm you're using the right kind of credential (see
     [Logging in](#logging-in)). For Kerberos mode, run
     `kinit <your-username>` on the dashboard host to test the
     domain/KDC independently of the dashboard.
3. **Page loads and you're logged in, but the data looks wrong,
   missing, or stale?**
   - Work through [Data freshness](#data-freshness): check disk space
     on `/linuxdb`, the last-modified time of the relevant data file,
     and the scheduled job's own log output.
4. **Got an alert email and want to investigate?**
   - See [Monitoring & Alerting](#monitoring-alerting) above for what
     each alert type means and where to look next.
5. **Everything above looks fine, but something still seems off?**
   - Note exactly which page, what you expected vs. what you actually
     saw, and roughly when you first noticed it, then check
     `journalctl -u log-search-ui` / `linuxdb-dashboard -n 200
     --no-pager` around that time — most issues leave a trace there
     even when nothing else points to an obvious cause.
