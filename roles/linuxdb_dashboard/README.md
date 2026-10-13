# Role: linuxdb_dashboard

## Purpose

Deploys a small, read-only web front end for browsing the linuxdb-wide
inventory produced by `roles/hostinfo` (`hostinfo.csv`) and drilling
down per host into the timestamped diagnostic history collected by
`roles/linuxdb_reports` (disk & filesystem, processes, server health,
system & hardware stats). Intended to run on the control node (the
`log_collector` inventory group, normally `admin1`), alongside
`roles/linuxdb_reports`. See `playbooks/linuxdb_dashboard.yml`.

Built entirely from **RHEL BaseOS/AppStream packages only** — the app
needs nothing beyond `python3` itself, which is standard on every RHEL
host. **No EPEL, no third-party repos, no pip installs, no
containers, no Flask** — `app.py` is a plain WSGI callable using only
the Python standard library, served by Python's own
`wsgiref.simple_server` under systemd. This mirrors the precedent set
by `roles/log_search_ui`'s standalone variant.

The app is compatible with RHEL 8's older `python3` as well as RHEL
10's — no walrus operator, no dict-merge `|` operator, nothing needing
3.8+/3.9+-only syntax.

## Behaviour

- Installs `python3` (present by default on RHEL, this is a no-op
  safety net).
- Creates an unprivileged system account (`linuxdb_dashboard_user`,
  default `linuxdb_dashboard`) with no login shell and no home
  directory.
- Deploys the application to `linuxdb_dashboard_app_dir` (default
  `/opt/linuxdb_dashboard`):
  - `app.py` — the WSGI application (deployed byte-for-byte via
    `copy`, **not** templated — see "Why app.py isn't templated"
    below).
  - `config.py` — paths and check registry (templated from Ansible
    variables — the only file where Ansible variables reach the app).
  - `wsgi_standalone_server.py` — a small threaded WSGI server runner
    (`socketserver.ThreadingMixIn` + `wsgiref`'s `WSGIServer`), reusing
    the pattern already built and tested for `roles/log_search_ui`.
- Deploys `/etc/systemd/system/linuxdb-dashboard.service` and
  enables/starts it. The unit hardens the process with
  `NoNewPrivileges`, `ProtectSystem=strict`, and `ProtectHome=yes` —
  safe here because the app never writes anywhere, it only reads
  `hostinfo.csv` and the `linuxdb_reports` data directory.
- Opens `linuxdb_dashboard_port/tcp` (default `8082`) in firewalld if
  firewalld is active and `linuxdb_dashboard_manage_firewalld` is true.
- When `linuxdb_dashboard_enabled` is `false`, stops/disables the
  service and removes the unit file and application directory (clean
  teardown, same pattern used by `roles/log_search_ui`).

## Why app.py isn't templated

`app.py` embeds a `<style>` block of plain CSS directly in the page
header, which contains literal `{` and `}` characters (CSS rule
blocks). Jinja2's `template` module would try to interpret those as
`{{ }}`/`{% %}` delimiters and corrupt the file. It's deployed with
`ansible.builtin.copy` instead, byte-for-byte, exactly as it exists in
this repo — the same approach used for `roles/log_search_ui`'s
`app.py`. `config.py` has no such characters (it's a list of
brace-free `(key, label)` tuples), so it's safe to render with
`template` and is the only place Ansible variables reach the running
app.

## The dashboard

- **Jump to a host by name (`/goto-host`)** — the Overview page opens
  with a "Jump to a host's reports" search box above the fleet table:
  a plain GET `<form>` (works with JavaScript disabled) with a text
  input, a `<datalist>` of every known hostname (built from the
  current `hostinfo.csv` rows plus any host under
  `linuxdb_dashboard_reports_data_dir` that has report history but no
  current `hostinfo.csv` row) for progressive-enhancement
  autocomplete, and a **View reports** submit button. Submitting posts
  to `GET /goto-host?hostname=<value>`, which 302-redirects straight
  to `/host/<value>` when the value passes the same hostname charset
  check (`is_valid_hostname()` / `HOSTNAME_RE`) used by `/host/`
  itself — deliberately *not* also requiring the host to already exist
  in `hostinfo.csv`, since `/host/<name>` already shows a friendly
  "no data yet" notice (with the exact `ansible-playbook ... --limit
  <host>` command to fix it) for a valid-but-unknown hostname. An
  invalid value (bad characters) redirects back to `/` with
  `?host_search_error=<value>`, which the Overview page shows as an
  inline notice next to the search box ("`<value>` doesn't look like a
  valid hostname — letters, numbers, dots and hyphens only.") instead
  of silently failing or 500ing. This replaces having to scroll/find a
  hostname in the table below and click its link — useful when you
  already know the exact host you need and the fleet table has grown
  past a quick visual scan.
- **Home page (`/`)** — shows a "hostinfo.csv last updated: <timestamp>"
  line (the live file's mtime) so it's obvious at a glance how stale
  the data is, followed by a **History** dropdown listing every
  timestamped snapshot `roles/hostinfo` has archived under
  `hostinfo/history/` (newest first, capped at
  `linuxdb_dashboard_max_history_list`). Picking one and clicking
  **View** reloads the page with `?snapshot=<timestamp>` and renders
  that archived `hostinfo_<timestamp>.csv` instead of the live file,
  with a banner ("Viewing archived snapshot from `<timestamp>`") and a
  **View latest** link back to the live view; the "last updated" line
  is only shown for the live view, since it isn't meaningful for an
  archived snapshot. An invalid or since-pruned `snapshot` value falls
  back to the live view with an error notice rather than a dead page.
  Then, the SUMMARY totals
  (`TOTAL_HOSTS`/`REACHABLE_HOSTS`/`UNREACHABLE_HOSTS`) from the
  selected run are shown, in a highlighted callout above
  the table so they aren't missed. Below that, a left-aligned table
  renders whatever columns exist in `hostinfo.csv`'s header dynamically
  (stays in sync automatically with `group_vars/all/host_checks.yml`,
  including its column heading text - short abbreviations like
  `CPUS`, `MEM_GB`, `MFR`, `FAILED_SVC_CNT` were chosen there
  deliberately to save horizontal space; see the comments in that file
  if you add a new check). Rows are always sorted alphabetically by `HOSTNAME`
  (case-insensitive), regardless of the order `hostinfo.yml` happened
  to process hosts in on its last run. That alphabetical-by-HOSTNAME
  order is just the initial sort though - every column header in this
  table is clickable, letting anyone viewing the dashboard re-sort by
  any field (click again to reverse direction; an arrow on the header
  shows the active column and direction). This is pure client-side
  JavaScript (`TABLE_SORT_JS` in `files/app.py`, injected via
  `PAGE_TAIL` alongside the existing scrollbar script) - no backend
  call, no page reload, and it doesn't touch `hostinfo.csv` or any
  other table on the dashboard (scoped to `.table-scroll table`, which
  only this Overview table uses). The three numeric columns
  (`CPUS`/`MEM_GB`/`SWAP_GB` - see `NUM_COL_HEADERS`) sort by parsed
  value; every other column, including `HOSTNAME` itself, sorts as
  text (the `HOSTNAME` cell's status badge is ignored - only the
  hostname link text is used as the sort key). The `HOSTNAME` cell in
  each row is itself the link to that host's detail page (there's no
  separate leading "Reports" column). Since `hostinfo.csv` can easily be
  25-30+ columns wide (every check in `group_vars/all/host_checks.yml`
  adds one), the table lives in a scrollable pane rather than trying
  to fit everything on screen at once:
  - The pane has an always-visible horizontal scrollbar (styled via
    `::-webkit-scrollbar`/`scrollbar-color` so it doesn't rely on a
    2-finger trackpad swipe to even appear, which is macOS's default
    for `overflow: auto`).
  - The `HOSTNAME` column is frozen (`position: sticky; left: 0;`) so
    it stays on screen while you scroll right through the rest of the
    columns - you never lose track of which host's row you're reading.
  - Cell text wraps (instead of forcing one long unbroken line) and is
    capped at a sensible max width, so more columns fit on screen
    before scrolling is needed at all.
  - The short, mostly-numeric columns (`CPUS`, `MEM_GB`, `SWAP_GB`) are
    centered and narrower than the rest, since a 1-3 digit value never
    needs a wide, left-aligned cell to display cleanly. This is
    matched by column heading text in `render_home()`
    (`NUM_COL_HEADERS` in `files/app.py`), not column position, so it
    keeps working if checks are reordered in
    `group_vars/all/host_checks.yml`.
  - With this many columns there's still no realistic way to fit every
    one on a normal screen without scrolling - these changes make the
    scrolling itself obvious and easy to use, and shrink the total
    table width as much as reasonably possible, rather than trying to
    eliminate scrolling entirely.
  - **Traffic-light status badge** — a small colour-coded badge
    (`OK`/`WARN`/`CRIT`, green/amber/red) sits right next to each
    row's `HOSTNAME` link, computed by `overview_row_status()` in
    `files/app.py` from whichever of a small set of well-known columns
    happen to exist in that environment's `hostinfo.csv`: any
    `FAILED_SVC_CNT > 0` is `CRIT`; a host that didn't report at all
    on the last `hostinfo.yml` run is also `CRIT` (its row is padded
    to the full column count with blank values rather than actually
    shorter, so this is detected by every field after `HOSTNAME` being
    blank, not by row length); `REBOOT REQD`, `DNF STATUS` other than `UP_TO_DATE`,
    `NTP_SYNC=no`, `IN SATELLITE=no`, `SELINUX=disabled`, and any of the
    named-service columns below not reporting their healthy value are
    each `WARN`. Hovering the badge shows exactly which of those
    tripped, as a native tooltip (`title=` attribute — no JavaScript).
    Matching is done by normalised column name, so it has no effect at
    all in an environment whose `hostinfo.csv` doesn't have a given
    column (e.g. a leaner `host_checks.yml`). The whole row is also
    lightly tinted to match (`.row-crit`/`.row-warn` in `PAGE_CSS`), so a problem host
    is visible before you've even read the badge text — the colour is
    never the only signal, the badge's text label always says the same
    thing the colour does.

    The badge also folds in that host's own **latest Disk & Filesystem
    check** result (`disk_check_overview_status()`, reading the same
    `df_check.sh` exit code the Disk & Filesystem tab already colours —
    see `roles/linuxdb_reports/README.md`'s "Disk & Filesystem" section
    for the WARN/CRIT utilisation thresholds themselves) — so a host
    whose `hostinfo.csv` columns are all clean can still show `WARN`/
    `CRIT` here purely because a filesystem crossed its usage
    threshold, with the tooltip pointing you at the Disk & Filesystem
    tab for the detail. If a host has no `df_check` data at all yet
    (report never run, or too new to have a `latest` pointer), this
    simply contributes nothing — same graceful-degradation approach as
    the `hostinfo.csv` columns above.

    The badge also folds in that host's latest **IdM healthcheck**
    result (`idm_healthcheck_overview_status()`, reading the same
    `ipa-healthcheck` JSON `roles/idm_healthchecks` writes when its own
    `idm_hc_dashboard_enabled` is true — see that role's README
    "Dashboard feed" section) — any `ERROR`/`CRITICAL` result is `CRIT`,
    any `WARNING` is `WARN`, so an IdM server with a replication
    conflict or a failed service shows up on the Overview page exactly
    like any other flagged host, with the tooltip pointing at the
    per-host "IdM Health" tab for detail. Like the Disk & Filesystem
    fold-in above, a host with no IdM healthcheck data at all (not an
    IdM server, or the check hasn't run yet) simply contributes
    nothing. There is also a fleet-wide "IdM Health" summary line above
    the main table (only shown at all if at least one host has IdM
    healthcheck data), naming every host currently reporting a problem
    — this works unchanged whether there's exactly one IdM server or
    several replicated ones, since it's driven purely by which host
    directories exist under the dashboard feed,
    never by a hardcoded count or group membership.

    It also checks a small table of **named-service status columns**
    (`SERVICE_STATUS_COLUMNS` in `files/app.py`) — `hostinfo.csv`
    columns that are themselves a `systemctl is-active <service>` (or
    equivalent) up/down check rather than a fact or setting. Today
    that's `CHRONYD`, `SSHD`, `FIREWALLD`, `AUDITD` and `CROND`
    (healthy value `active` on all five — these are the same five
    always-on daemons `linuxdb_reports`' `svr_check.sh` used to check
    separately; that duplicate check was removed once it was added
    here, so this is now the only place their up/down state is
    monitored), `RSM222` and `RSM232` (healthy value `active`, only
    populated on hosts in their own `rsm222_hosts`/`rsm232_hosts` list
    — see `group_vars/all/host_checks.yml`), and `FLEXLM` (healthy
    value `running`, only populated on `flexlm_hosts`). Any other
    value — `inactive`, `failed`, `activating`, etc. — is `WARN`, e.g.
    stopping `chronyd` on a host and re-running `hostinfo.yml` shows
    `WARN` with a "Chronyd not running (inactive)" tooltip on that
    host's very next Overview refresh, no `linuxdb_reports` re-run
    needed. A blank value (a host not in the relevant site list) is
    skipped, same as any other inapplicable column. Add a new row to
    `SERVICE_STATUS_COLUMNS` to cover another `systemctl is-active`-
    style column you add to `host_checks.yml` (e.g. the commented-out
    `POSTGRESQL_STATUS` example there).

    Every offending value that trips any of the checks above — not just
    the named-service ones — is also highlighted directly in its own
    cell: bold red text on a red background (`.cell-alert` in
    `PAGE_CSS`), applied to the exact cell holding e.g. `inactive` in a
    `CHRONYD` column, `Yes` in `REBOOT REQD`, or `UPDATE_REQUIRED` in
    `DNF STATUS`. This is on top of, not instead of, the row tint and
    badge — someone glancing at the wide Overview table (or the
    per-host detail page, which highlights the same way) sees the exact
    problem value even if they don't notice or hover the badge.
  - **Download this table as CSV** — a link above the table (respects
    the History dropdown's `?snapshot=` selection, so an archived
    snapshot can be exported too, not only the live file) streams the
    exact same rows/columns back out as a plain `.csv` via a new
    `/export.csv` route, using the standard library's `csv` module —
    no new dependency. Short (unreachable-host) rows are padded with
    empty cells the same way the HTML table pads them.
  The UNREACHABLE_HOST_LIST from the last `hostinfo.yml` run is shown
  right under the `TOTAL_HOSTS`/`REACHABLE_HOSTS`/`UNREACHABLE_HOSTS`
  callout (not after the table), one hostname per line rather than
  comma-separated so a run of several down hosts can't be misread as
  one long name. Any host with `linuxdb_reports` history but no
  current `hostinfo.csv` row is listed separately below the table.
- **Host detail (`/host/<hostname>`)** — shows the host's `hostinfo.csv`
  row as an escaped key:value table, then one tab per configured check
  (`?check=<key>`). Each tab has a plain `<form method="get">` history
  dropdown (`?ts=<timestamp>` or `latest`, no JavaScript anywhere in
  the app) and shows the check's exit code, colour-coded
  (green/amber/red). Checks with no data yet show the exact
  `ansible-playbook` command to run to collect them.
- **Per-check rendering**:
  - `svr_check` / `dnf_updates` — parsed into one colour-coded status
    table (PASS/INFO=green/blue, WARNING=amber, CRITICAL=red), matched
    by column name so it stays correct even if columns are reordered.
    Both emit a flat `HOSTNAME|CHECK|STATUS|VALUE` shape end-to-end, no
    non-tabular remainder — for `dnf_updates` that means every pending
    package, the `REBOOT_REQUIRED` row and the final `SUMMARY` row all
    render as ordinary colour-coded table rows.
  - `df_check` / `proc_check` / `system_stats` — the leading
    pipe-delimited table is parsed and rendered as a table (same
    colour-coded status column as `svr_check`); everything after it
    (WARNINGS, SUMMARY, TOP_CPU/TOP_MEMORY sections for df_check/
    proc_check; the full `nmcli device show` + `ip -br a` output for
    system_stats — none of which is pipe-delimited) is shown as
    preformatted raw text. For `df_check`
    specifically, the mountpoints already named in the "FILESYSTEM
    UTILISATION WARNINGS" text block underneath (`CRITICAL|mountpoint|
    pct%|fs` / `WARNING|mountpoint|pct%|fs`) also get their `USE%` cell
    in the table above bold-red highlighted (`.cell-alert`, same class
    the Overview/host-detail pages use) — so the actual over-threshold
    percentage is visible directly in the table, not only named in the
    plain text below it. A filesystem excluded from alerting by
    `df_check.sh`'s own pseudo-filesystem filter (tmpfs, squashfs, etc.
    — see `roles/linuxdb_reports/README.md`) never appears in that
    warnings block, so its `USE%` is correctly never highlighted here
    either, even at 100%.
  - `rpm_report` — the leading (and only) pipe-delimited table is
    parsed and rendered as a plain table, same as `df_check` above but
    with no `STATUS` column and no colour coding at all — this is an
    inventory listing, not a threshold check, so there's nothing to
    flag (see `roles/linuxdb_reports/README.md`'s "Installed Packages"
    section). This is also the per-host counterpart to the fleet-wide
    **Package Search** page below — same underlying data, one host at
    a time instead of searched across all of them.
  - Anything else (any future custom check) is shown as preformatted
    raw text — a safe default that needs no code change when a new
    check is added to the registry.
- **Config Files tab** — an extra tab, always shown last, populated
  from `roles/config_snapshot` data rather than `linuxdb_reports_checks`
  (so it needs no entry in that registry). Lists every config file
  captured for the host, each with a status badge — **First run**,
  **Unchanged**, **Changed**, **Now missing**, **File appeared**, or
  **Missing** — comparing the two most recent runs, reusing the exact
  same badge styling as every other check (`STATUS_CLASS`), so this
  needed no new CSS. Clicking a file shows its full content for the
  selected run (plain `<pre>`, escaped) with its sha256 and the same
  plain-GET history dropdown pattern used elsewhere, and a file with no
  snapshots yet shows the exact `ansible-playbook` command to collect
  it, same as an empty check tab does. A file recorded as missing at a
  given run (the file didn't exist on that host at that time — e.g.
  `/etc/exports` on a host that isn't an NFS server, or
  `/etc/oracle-release` on RHEL rather than Oracle Linux) shows a plain
  notice instead of file content for that run.
- **IdM Health tab** — an extra tab, populated from
  `roles/idm_healthchecks`' dashboard feed (`IDM_HEALTHCHECK_DATA_DIR`)
  rather than `linuxdb_reports_checks`. Unlike the Config Files tab
  above, this one is **not shown at all** for a host with no IdM
  healthcheck data — it only appears once that host has at least one
  recorded run, so the tab bar stays uncluttered for the rest of the
  fleet. Shows the same plain-GET run-history dropdown pattern as every
  other tab, a one-line pass/warning/critical summary count for the
  selected run, and — when there's anything to show — a table of every
  finding that wasn't `SUCCESS` (source, check, a colour-coded
  PASS/WARNING/CRITICAL badge reusing `render_status_badge()`, and the
  message `ipa-healthcheck` itself reported). A run with everything
  passing shows a plain "all checks passed" notice instead of an empty
  table. A host with no data yet shows the exact `ansible-playbook
  playbooks/idm_healthcheck.yml --limit <host>` command to collect it,
  same convention as every other empty check tab.
- **Package Search (`/packages`)** — fleet-wide "which servers have
  package X installed?" search, reachable from a "Package Search" tab
  in the header switcher on every page (see "Cross-app navigation
  switcher" below), not tied to any one host. A plain GET search box
  (`search_installed_packages()` in `app.py`) reads every host's
  *already-collected* latest `rpm_report` run — the same data behind
  each host's own Installed Packages tab — and does a case-insensitive
  substring match against the package name, so searching "firefox"
  returns every host with a matching package installed, each with its
  exact version/release/architecture and which run the data came from.
  Nothing here queries any managed host directly or triggers a new
  collection; if a host has no `rpm_report` data yet, it's silently
  excluded from results rather than erroring. Results render as a
  sortable table (same `.table-scroll` + clickable-header pattern as
  the Overview fleet table — see "Home page" above), with each
  `HOSTNAME` cell linking straight to that host's own Installed
  Packages tab. A deployment with no `rpm_report` check configured at
  all (e.g. before this feature was rolled out) shows a friendly
  notice instead of a broken search, and a query with zero matches
  shows the exact `ansible-playbook ... --tags linuxdb_reports`
  command to collect fresh data instead of a bare empty page.
- **Operational guide (`/guide`)** — serves the team-facing
  `OPERATIONAL_SUPPORT_GUIDE.md` (repo root) as a styled static page inside
  the dashboard, so the team that uses LinuxDB day-to-day has the guide one
  click away (a "Guide" tab sits in the header switcher on every page — see
  "Cross-app navigation switcher" below) without needing SSH or repo
  access. This is a
  **build-time** conversion, not a live one: `docs/render_guide.py` (run by
  a human, from a checkout with `markdown` installed — `pip install
  markdown` in a venv if needed) converts the `.md` source to HTML once and
  writes the result to `roles/linuxdb_dashboard/files/guide_content.html`,
  which is committed to the repo like any other file and deployed by the
  "Deploy the pre-rendered operational guide" task in `tasks/main.yml` (a
  plain `ansible.builtin.copy`, same pattern as the other static files this
  role ships — no `markdown` Python package or any other new dependency
  needed on the dashboard host itself, keeping `app.py`'s zero-dependency,
  pure-stdlib design intact). `render_guide()` in `app.py` reads that
  pre-rendered HTML once at process start and serves it wrapped in the same
  page chrome (header, nav, CSS) as every other page; if the file is
  missing (e.g. `docs/render_guide.py` was never run before a deploy), the
  route still renders the normal page chrome with a plain notice instead of
  a 500 error. **Whenever `OPERATIONAL_SUPPORT_GUIDE.md` changes, re-run
  `python3 docs/render_guide.py` from the repo root and commit the
  regenerated `guide_content.html` alongside it** — the dashboard has no way
  to know the source changed and will otherwise keep serving the old
  content after the next deploy.

## Cross-app navigation switcher

The header on every LinuxDB Dashboard page (Overview, per-host detail,
IdM Health, Package Search) and on the `/guide` page shows the same
tab-style switcher (`_app_switcher_html()` in `app.py`), so any page in
either app can jump straight to any of the five operational web pages
without needing to remember a URL - every tab navigates in the same
browser tab, none of them open a new one:

| Tab                | Destination                                | Always shown? |
| ------------------ | ------------------------------------------- | -------------- |
| LinuxDB Dashboard  | `/` (this app)                               | Yes            |
| Log Search         | the sibling app's own home page              | Only if configured |
| Insights           | the sibling app's `/insights` page           | Only if configured |
| Package Search     | `/packages` (this app)                       | Yes            |
| Guide              | `/guide` (this app)                          | Yes            |

LinuxDB Dashboard, Package Search and Guide are always present (all
three live in this app), and Package Search's position in the bar never
moves — it always sits right after Log Search/Insights and right before
Guide, whether or not the sibling app is configured. Log Search and
Insights only appear once the sibling app's URL is configured — set
`linuxdb_dashboard_sibling_app_name` / `linuxdb_dashboard_sibling_app_url`
(see `defaults/main.yml`); the Insights tab is derived automatically
from the same URL (its `/insights` path), so there's no separate
variable to set for it. This stays additive: an unconfigured deployment
just shows LinuxDB Dashboard, Package Search and Guide, exactly as
before this feature existed.

`group_vars/all/linuxdb_dashboard.yml` sets the sibling app variables for
this repo's own home-lab layout, pointing at the same control node's Log
Search port — so both extra tabs show up out of the box here. Log Search's
own header switcher mirrors this back the other way (see
`roles/log_search_ui/README.md`'s "Cross-app navigation switcher"
section), so every page in either app can reach every other page.

## Security

- **Read-only.** This process never writes to `hostinfo.csv`, its
  `history/` snapshots, or the `linuxdb_reports` data directory — every
  filesystem access in `app.py` is an open-for-read.
- Hostnames, check keys, and timestamps taken from the URL — including
  the Overview page's `?snapshot=<timestamp>` query param — are
  validated against strict patterns (`HOSTNAME_RE`, `TIMESTAMP_RE`,
  the check registry) before being used to build any path.
- Every candidate path is resolved with `os.path.realpath()` and
  checked that it still lives under the configured data directory
  before the file is opened — defence in depth against path traversal,
  even though the earlier pattern validation should already prevent
  it.
- Every dynamic value written into HTML output is passed through
  `html.escape()`.

## Authentication

The dashboard sits directly on the open internet-facing port
(`linuxdb_dashboard_port`, default `8082`) with **no reverse proxy** in
front of it - instead, HTTP Basic Auth is checked directly inside
`app.py` itself (`_check_basic_auth()`, right at the top of the WSGI
`application()` entry point), using only the Python standard library
(`base64` + `hmac.compare_digest()` for a timing-safe comparison) - no
httpd, no mod_wsgi, no Flask-Login, no session/cookie state. Every
request re-sends and re-checks credentials.

- Controlled by `linuxdb_dashboard_auth_enabled` (default `true`).
  When `true`, every request without a valid `Authorization: Basic
  ...` header gets a `401 Unauthorized` with a `WWW-Authenticate:
  Basic` challenge header - the browser then prompts for a
  username/password itself, no login page needed.
- The username/password are `linuxdb_dashboard_auth_user` /
  `linuxdb_dashboard_auth_password`. By default these fall back to
  the shared `dashboard_auth_user` / `dashboard_auth_password`
  variables defined in `group_vars/all/dashboard_auth.yml` - **one
  credential protects both this dashboard and `roles/log_search_ui`**
  by design. Override `linuxdb_dashboard_auth_user`/`_password`
  directly (in `group_vars/all/linuxdb_dashboard.yml` or a
  host/group-specific file) instead if you want this dashboard to use
  a different credential than `log_search_ui`.
- **Change the password before using this outside a throwaway lab** -
  vault it the same way as the existing DB passwords elsewhere in this
  repo:

  ```
  ansible-vault create group_vars/all/dashboard_auth_secrets.yml
  ```
  ```yaml
  dashboard_auth_password: "a real password here"
  ```

  `group_vars/all/*_secrets.yml` is already excluded from git by
  `.gitignore`, and Ansible merges all of `group_vars/all/*.yml`
  together, so the vaulted value naturally overrides the plain
  default in `group_vars/all/dashboard_auth.yml`.
- To reset the password later, just change
  `dashboard_auth_password` (or the role-specific override) and
  re-run the playbook - `config.py` is re-templated and the service
  restarts via the existing `notify: Restart linuxdb-dashboard`
  handler, same as any other config change.

### Kerberos authentication mode

Instead of the local username/password above, you can validate each
login against a real Kerberos realm - FreeIPA/IdM or Active Directory,
both are just Kerberos KDCs under the hood, so the same code path works
against either. The browser still shows the same Basic Auth
username/password prompt - nothing changes for the end user - but
`app.py` verifies the submitted credentials with a throwaway `kinit`
instead of comparing against a locally-stored password.

- Set `linuxdb_dashboard_auth_mode` (or the shared `dashboard_auth_mode`
  in `group_vars/all/dashboard_auth.yml`) to `kerberos`. Default is
  `local` - the existing behaviour above, completely unchanged.
- Set `linuxdb_dashboard_auth_kerberos_realm` (or the shared
  `dashboard_auth_kerberos_realm`) to your realm name, e.g.
  `LAB.EXAMPLE.COM` or `CORP.EXAMPLE.COM`. **Required** when kerberos
  mode is enabled - the role fails the deploy with a clear message if
  it's left empty, and the app itself fails closed (denies every login)
  if somehow started without one.
- **Prerequisite: the target host must already be domain-joined**
  (`realmd`/SSSD, or an IdM↔AD trust) with `krb5-workstation` already
  installed. This role **never installs krb5-workstation for you** - it
  only *checks for* `kinit`/`kdestroy` at deploy time (same "check for,
  never install" pattern used elsewhere in this repo for `kinit`/
  `getcert`/`sssctl`) and fails the deploy with a clear message if
  they're missing.
- How it works, per login attempt: the submitted username/password are
  used to run `kinit <user>@<realm>` with the password piped in via
  stdin (never an argv element, never visible in `ps`, never written to
  disk) into a dedicated, throwaway ticket cache file. The exit code
  determines pass/fail, and the cache is destroyed (`kdestroy`) and
  removed immediately afterwards, every time - see
  `_check_kerberos_password()` in `app.py`.
- **Optional group gating**: set `linuxdb_dashboard_auth_kerberos_required_group`
  (or the shared `dashboard_auth_kerberos_required_group`) to a
  POSIX/AD group name to additionally require the authenticated user to
  be a member of that group - checked via `getent group` (supplementary
  members) and `id -nG` (also covers the user's primary group), both
  backed by the same SSSD cache already used for name resolution. Leave
  it empty (the default) to allow any successfully-authenticated
  principal in the realm, with no group restriction.
- Usernames are validated against `^[A-Za-z0-9_.-]{1,64}$` before ever
  being handed to `kinit`/`getent`/`id` - anything else is rejected
  immediately, with no subprocess call made at all.
- Override `*_kinit_path`/`*_kdestroy_path`/`*_getent_path`/`*_id_path`
  only if your host installs these somewhere non-standard - the
  defaults (`/usr/bin/kinit` etc.) match every current RHEL release.
- `linuxdb_dashboard_auth_user`/`_password` (the local-mode credential)
  are simply ignored while `linuxdb_dashboard_auth_mode` is `kerberos`.

**Before enabling this against a real environment, confirm:**

1. The host is domain-joined - check with `realm list` or
   `sssctl domain-list`.
2. A manual `kinit <user>@<realm>` already succeeds from that host
   today (proves KDC reachability and `/etc/krb5.conf` realm mapping).
3. `id <user>` resolves cleanly via SSSD (proves NSS/group lookups
   work) - and, if using group gating, that the group name you plan to
   use in `linuxdb_dashboard_auth_kerberos_required_group` is correct
   (`getent group <group>`).
4. The exact UPN/username format your realm expects for `kinit` -
   plain `user`, `user@REALM`, or `DOMAIN\user` - some environments
   restrict interactive password auth on service-style accounts, so
   also confirm the account you plan to use actually allows it.

## TLS

The standalone `wsgiref.simple_server` socket in
`wsgi_standalone_server.py` is wrapped directly in TLS using the
standard library's own `ssl` module
(`ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).wrap_socket(...)`) - same
port (`linuxdb_dashboard_port`), no new port, no firewalld change, no
httpd/mod_ssl, no reverse proxy. The dashboard serves plain
`https://` instead of `http://` on that same port once TLS is enabled.

- Controlled by `linuxdb_dashboard_tls_enabled` (default `true`).
- **Self-signed certificate, generated automatically on first
  deploy** if `linuxdb_dashboard_tls_generate_self_signed` is `true`
  (default) and no cert already exists at
  `linuxdb_dashboard_tls_cert_path` - a single `openssl req -x509`
  command task guarded by `creates:`, so it only ever runs once per
  host and is left alone (idempotent) on every later run. No
  `community.crypto` collection dependency - `requirements.yml` stays
  unchanged.
- Cert/key paths default to
  `{{ linuxdb_dashboard_app_dir }}/tls/linuxdb-dashboard.{crt,key}`
  (`linuxdb_dashboard_tls_cert_path` / `_key_path`). The private key
  is always `0600`, owned by `linuxdb_dashboard_user`.
- The self-signed cert's Common Name defaults to the target host's
  own FQDN (`linuxdb_dashboard_tls_cn`, `ansible_fqdn` fact) and is
  valid for `linuxdb_dashboard_tls_cert_days` (default `3650` days).
- Because the cert is self-signed, browsers and `curl` will warn about
  an untrusted certificate (`curl -k` / "Advanced -> Proceed" in a
  browser) unless you import it as a trusted cert yourself, or supply
  your own CA-signed cert/key at `linuxdb_dashboard_tls_cert_path` /
  `_key_path` before the first deploy (the role only generates one if
  the file doesn't already exist - it never overwrites a cert you
  provide).
- If you ever need a fresh cert (e.g. it expired, or the hostname
  changed), delete the file at `linuxdb_dashboard_tls_cert_path` and
  re-run the playbook - a new one is generated the same way.

## Variables

| Variable | Default | Description |
|---|---|---|
| `linuxdb_dashboard_enabled` | `false` | Deploy and run the service. When `false`, any existing deployment is torn down. |
| `linuxdb_dashboard_hostinfo_csv_path` | `/linuxdb/hostinfo/hostinfo.csv` | Full path to `hostinfo.csv`. Must match `roles/hostinfo`'s `host_report_csv_path` + `/hostinfo.csv`. The Overview page's history dropdown reads the sibling `history/` directory (`roles/hostinfo`'s `host_report_keep_runs`) — no separate variable needed here, since it's always that path's own directory + `/history`. |
| `linuxdb_dashboard_max_history_list` | `30` | Max number of timestamped runs offered per check's history dropdown, and per the Overview page's hostinfo history dropdown. |
| `linuxdb_dashboard_port` | `8082` | TCP port the standalone server listens on. |
| `linuxdb_dashboard_app_dir` | `/opt/linuxdb_dashboard` | Where `app.py`, `config.py`, and the server runner are deployed. |
| `linuxdb_dashboard_user` | `linuxdb_dashboard` | Unprivileged system account the service runs as. |
| `linuxdb_dashboard_manage_firewalld` | `true` | Open `linuxdb_dashboard_port/tcp` in firewalld if firewalld is active. |
| `linuxdb_dashboard_auth_enabled` | `true` | Require HTTP Basic Auth on every request - see "Authentication" above. |
| `linuxdb_dashboard_auth_user` | falls back to `dashboard_auth_user` (`admin`) | Basic Auth username. |
| `linuxdb_dashboard_auth_password` | falls back to `dashboard_auth_password` (`CHANGE_ME_dashboard_admin`) | Basic Auth password - vault this, see "Authentication" above. |
| `linuxdb_dashboard_auth_mode` | falls back to `dashboard_auth_mode` (`local`) | `local` (password above) or `kerberos` - see "Kerberos authentication mode" above. |
| `linuxdb_dashboard_auth_kerberos_realm` | falls back to `dashboard_auth_kerberos_realm` (`""`) | Kerberos realm to authenticate against when auth mode is `kerberos`. Required in that mode. |
| `linuxdb_dashboard_auth_kerberos_required_group` | falls back to `dashboard_auth_kerberos_required_group` (`""`) | Optional POSIX/AD group required for access in `kerberos` mode. Empty = any valid principal. |
| `linuxdb_dashboard_auth_kerberos_kinit_path` | falls back to `dashboard_auth_kerberos_kinit_path` (`/usr/bin/kinit`) | Path to `kinit`, used in `kerberos` mode. |
| `linuxdb_dashboard_auth_kerberos_kdestroy_path` | falls back to `dashboard_auth_kerberos_kdestroy_path` (`/usr/bin/kdestroy`) | Path to `kdestroy`, used in `kerberos` mode. |
| `linuxdb_dashboard_auth_kerberos_getent_path` | falls back to `dashboard_auth_kerberos_getent_path` (`/usr/bin/getent`) | Path to `getent`, used for group gating in `kerberos` mode. |
| `linuxdb_dashboard_auth_kerberos_id_path` | falls back to `dashboard_auth_kerberos_id_path` (`/usr/bin/id`) | Path to `id`, used for group gating in `kerberos` mode. |
| `linuxdb_dashboard_tls_enabled` | `true` | Serve HTTPS instead of HTTP on `linuxdb_dashboard_port` - see "TLS" above. |
| `linuxdb_dashboard_tls_generate_self_signed` | `true` | Auto-generate a self-signed cert on first deploy if one doesn't already exist. |
| `linuxdb_dashboard_tls_cn` | target host's `ansible_fqdn` | Common Name on the generated self-signed cert. |
| `linuxdb_dashboard_tls_cert_days` | `3650` | Validity period (days) of the generated self-signed cert. |
| `linuxdb_dashboard_tls_cert_path` | `{{ linuxdb_dashboard_app_dir }}/tls/linuxdb-dashboard.crt` | Path to the TLS certificate. |
| `linuxdb_dashboard_tls_key_path` | `{{ linuxdb_dashboard_app_dir }}/tls/linuxdb-dashboard.key` | Path to the TLS private key (mode `0600`). |

This role also reads (but does not define) `linuxdb_reports_data_dir` and
`linuxdb_reports_checks` from `roles/linuxdb_reports`,
`config_snapshot_data_dir` from `roles/config_snapshot`, and
`idm_hc_dashboard_dir` from `roles/idm_healthchecks` (see that role's
README "Dashboard feed" section), so all of these roles always agree on
where data lives and which checks exist — see
`group_vars/all/linuxdb_dashboard.yml`. It also reads `dashboard_auth_user`
and `dashboard_auth_password` from `group_vars/all/dashboard_auth.yml`
(shared with `roles/log_search_ui`) unless overridden as above.

## Usage

```
ansible-playbook playbooks/linuxdb_dashboard.yml --tags linuxdb_dashboard
```

Then browse to `https://<admin1>:8082/` (TLS is on by default - see
"TLS" above; `curl -k` or "proceed anyway" in a browser is expected
for the self-signed cert) and log in with the Basic Auth credentials
from `group_vars/all/dashboard_auth.yml` (see "Authentication" above).
Run `--tags linuxdb_reports` and
`--tags config_snapshot` first (or alongside, with no `--tags` at all)
so there's actual data to view — see `roles/linuxdb_reports/README.md`
and `roles/config_snapshot/README.md`.

## Requirements

- `python3` (base RHEL, no extra repos).
- A `hostinfo.csv` produced by `playbooks/hostinfo.yml` and/or report
  history produced by `playbooks/linuxdb_dashboard.yml --tags
  linuxdb_reports` — the dashboard renders sensible "no data yet"
  messages with the exact command to run if either is missing, rather
  than erroring.
- The `/linuxdb` mount present and writable on the control node before
  the first run — `hostinfo.csv` (`/linuxdb/hostinfo/hostinfo.csv` by
  default), the `linuxdb_reports` data, and `config_snapshot` data all
  live under it. `linuxdb_dashboard_hostinfo_csv_path` must always
  match `roles/hostinfo`'s `host_report_csv_path` + `/hostinfo.csv`,
  or this role will show "No hostinfo.csv found" even though one
  exists elsewhere.
