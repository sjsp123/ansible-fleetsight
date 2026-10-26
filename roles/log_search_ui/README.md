# Role: log_search_ui

## Purpose

Deploys a small, dependency-free web front end for searching the log_db
database (`roles/log_db`) by message pattern, host, tag, severity, and
date range — so log messages across the fleet can be searched from one
place instead
of grepping flat files on (or across) 160+ servers. Intended to run on
the same host as `roles/log_db` (the `log_collector` inventory group).
See `playbooks/centralized_logging.yml`.

Built entirely from **RHEL BaseOS/AppStream packages only** —
`python3-psycopg2` — with **no EPEL, no third-party repos, no pip
installs, no containers**, so it patches via `dnf update` on a
subscribed RHEL host with just the standard repos enabled. The app
itself is a small plain WSGI callable using only the Python standard
library, served by Python's own `wsgiref.simple_server` — no httpd, no
mod_wsgi, and (deliberately) no Flask: Flask isn't published in RHEL 9's
own repositories (it's EPEL-only there).

## Behaviour

- Installs `python3-psycopg2`.
- Creates an unprivileged system account (`log_search_ui_user`, default
  `log_search_ui`) to run the service as.
- Deploys the application to `log_search_ui_app_dir` (default
  `/opt/log_search_ui`):
  - `app.py` — the WSGI application (deployed byte-for-byte via `copy`,
    **not** templated — see "Why app.py isn't templated" below).
  - `config.py` — database connection settings (templated from Ansible
    variables — the only file where Ansible variables reach the app).
  - `wsgi_standalone_server.py` — a small multi-threaded stdlib WSGI
    server (`wsgiref.simple_server` + `socketserver.ThreadingMixIn`)
    that imports and serves `application` from `app.py`.
- Deploys a systemd unit (`log-search-ui.service`) that runs
  `wsgi_standalone_server.py`, and enables/starts it.
- Opens `log_search_ui_port/tcp` (default `8081`) in firewalld if
  firewalld is active.
- Cleans up anything left behind by an older version of this role that
  also deployed an httpd + mod_wsgi variant side by side (removes its
  vhost config and stops/removes its `-standalone`-named service unit
  and separate app directory, if present) — see "Removing httpd" below
  if you're converging an already-deployed host.

## The search UI

A single page with a form (message pattern, host, tag, severity,
since/until) and a results table. Two match modes for the message field:

- **Contains** — case-insensitive substring match (`ILIKE '%...%'`),
  uses the `pg_trgm` GIN index from `roles/log_db` for speed.
- **Regex** — POSIX case-insensitive regex (`~*`), for pattern searches
  flat-file `grep -E` users will find familiar.

Results are paginated: a "Results per page" pulldown offers
`log_search_ui_max_results` (default `500`) plus its 2x/4x/10x tiers
(so 500/1000/2000/5000 by default), with Previous/Next links that
preserve every current filter and only change the page. An "All" option
fetches everything matching in one page (no Previous/Next) up to the
separate `log_search_ui_max_results_all` safety cap (default `20000`) —
use it when you want the full result set at once instead of paging
through it in stages.

The server handles each request in its own thread
(`socketserver.ThreadingMixIn`), so one slow or broad search doesn't
block a second concurrent request — unlike a plain
`wsgiref.simple_server`, which handles one request at a time by default.
This is safe here because every request is stateless: no shared mutable
state, and each request opens its own fresh database connection rather
than sharing one across threads.

## Cross-app navigation switcher

The header on every Search/Insights page shows a single switcher
(`render_nav()`, embedded via `page_head()` next to the "Log Search"
heading - same header layout as `linuxdb_dashboard`'s own switcher) that
reaches all five operational web pages from wherever you currently are,
always navigating in the same tab:

| Tab                 | Destination                                        | Always shown? |
| ------------------- | --------------------------------------------------- | -------------- |
| LinuxDB Dashboard   | the sibling app's own home page                      | Only if configured |
| Search              | `/` (this app)                                       | Yes            |
| Insights            | `/insights` (this app)                               | Yes            |
| Package Search      | `/packages` on the LinuxDB Dashboard                  | Only if configured |
| Guide               | `/guide` on the LinuxDB Dashboard                     | Only if configured |

Search and Insights are always present (both live in this app, so the
switcher can always link straight to `/` and `/insights`). LinuxDB
Dashboard, Package Search and Guide are each independently opt-in and
blank/off by default, so a fresh deployment never assumes anything
about what else is deployed, and Package Search's position never moves
- it always sits right after Insights and right before Guide, whether
or not either of those is actually configured:

- **LinuxDB Dashboard** — set `log_search_ui_sibling_app_name` /
  `log_search_ui_sibling_app_url` (see `defaults/main.yml`) to enable it.
- **Package Search** — a link to the fleet-wide installed-package search,
  which is hosted at `/packages` on the LinuxDB Dashboard rather than here
  (same one-app-hosts-it reasoning as Guide below — see
  `roles/linuxdb_dashboard/README.md`'s "Package Search (`/packages`)"
  section). Set `log_search_ui_packages_app_url` (see `defaults/main.yml`)
  to enable it.
- **Guide** — a link to the Operational Support Guide
  (`OPERATIONAL_SUPPORT_GUIDE.md`), which is hosted at `/guide` on the
  LinuxDB Dashboard rather than here (one guide covers both apps, so it
  only needs hosting in one place — see `roles/linuxdb_dashboard/README.md`'s
  "Operational guide (`/guide`)" section). Set `log_search_ui_guide_app_url`
  (see `defaults/main.yml`) to enable it.

`group_vars/all/log_db.yml` sets all three for this repo's own home-lab
layout, pointing at the same control node's LinuxDB Dashboard port — so
all three tabs show up out of the box here. The LinuxDB Dashboard's own
header switcher mirrors this back the other way (see
`roles/linuxdb_dashboard/README.md`'s "Cross-app navigation switcher"
section), so every page in either app can reach every other page.

## The Insights page

A second page, `/insights`, alongside the search page — linked from the
same nav bar at the top of both. It answers "what's actually going on in
the logs right now" at a glance, over a selectable time window
(`1h` / `24h` / `7d` / `30d`, default `log_search_ui_insights_default_window`),
without writing an ad hoc query by hand every time:

- **KPI counts** for the chosen window: SSH authentication failures,
  sudo commands run, password failures across all sources (SSH, sudo,
  `su`), and the number of distinct source IPs behind failed SSH logins.
  Each card also shows a **trend delta** against the immediately
  preceding window of the same length (e.g. for a `24h` window, the
  24h block right before it) — "SSH auth failures: 12 (up +140% vs
  previous 24h)" — computed by simply running the exact same KPI query
  twice (once for the current window, once for the one before it) and
  diffing the two counts in Python; no new query shape, no extra config
  variable, it just reuses `log_search_ui_insights_default_window`'s
  length automatically. A brand-new pattern with zero occurrences in
  the previous window shows "New (0 in previous window)" instead of a
  percentage (dividing by zero isn't meaningful), and an unchanged
  count shows "No change vs previous <window>".
- **Per-host scoping** — an optional `host=` filter, reusing the exact
  same tolerant case-insensitive substring match (`fromhost ILIKE
  '%...%'`) that the Search page's own host filter already validates.
  Leave it blank for the existing fleet-wide view. Enter a hostname (or
  fragment of one) and every KPI card, chart, and table on the page —
  plus both CSV exports and every drill-down link into Search — narrows
  to just that host, so you can ask "what's going on with just this one
  box" without leaving the page. A "Clear host filter" link appears
  next to the field whenever a host filter is active.
- **Severity breakdown** — a count per syslog severity (Emergency down
  to Debug) in the window, from the `priority` column populated by
  `roles/rsyslog_collector` (plain 0–7 severity, not the combined PRI
  value).
- **Top repeated / similar alerts** — the window's most-recent rows (up
  to `log_search_ui_insights_scan_rows`, a hard safety cap independent of
  window size) are normalized (IPs, ports, PIDs, UIDs, and usernames
  replaced with placeholders like `<IP>`/`<PORT>`) and grouped, so 30
  "Failed password for alice from 203.0.113.55 port 40001" /
  "...for bob from 203.0.113.55 port 40002" / etc. lines collapse into
  one row showing the shared pattern, a sample message, how many times
  it occurred, and how many distinct hosts/source IPs it touched.
- **Top source IPs (failed SSH logins)** — the source IPs with the most
  failed SSH attempts in the window, with distinct usernames tried and
  distinct target hosts, to spot both classic brute-force (many attempts,
  one username) and credential-stuffing (many usernames, few attempts
  each) shapes.
- **Top sudo activity** — who ran `sudo`, which account they became, and
  what they ran, grouped by invoking user / target ("becoming") user /
  base command (up to `log_search_ui_insights_top_sudo_users` rows), so
  e.g. three separate `systemctl restart httpd` calls by the same person
  collapse into one row with a count of 3 instead of three singletons.
  This is the detailed breakdown behind the "Sudo commands run" KPI card
  above (clicking that card jumps straight down to this table). Each row
  shows a sample full command line and a per-row drill-down link built
  from the exact invoking user / target user / command combination.
- **Top su activity** — who ran `su`, and which account they became,
  grouped by invoking user / target ("becoming") user (up to
  `log_search_ui_insights_top_su_users` rows). Unlike sudo, `su` has no
  per-command log of its own — its own PAM session-open line (e.g.
  `pam_unix(su-l:session): session opened for user root(uid=0) by
  sarb(uid=1000)`) only records who switched to who, since `su` just
  opens an interactive shell rather than logging each command run
  afterwards — so this table has no "Command" column, and there's no
  equivalent "su commands run" KPI card (a bare-tag `su%` filter would
  also match `sudo:`/`sudo[pid]:` tags, so the query instead matches only
  `su:`/`su[pid]:` tags with a "session opened" message).
- **Genuine / Review / Likely noise badges** — each repeated-alert
  pattern, each top source IP, each sudo-activity row, and each
  su-activity row gets a rough heuristic verdict with a plain-English
  rationale, computed in Python (never in SQL) from the small result
  sets above:
  - **Genuine?** — an authentication-failure pattern seen from at least
    `log_search_ui_insights_repeat_ip_threshold` distinct source IPs, or
    at least `log_search_ui_insights_bruteforce_attempts_threshold` times
    total (possible brute-force/credential-stuffing); or any Error-or-worse
    severity (0–3) pattern recurring at least
    `log_search_ui_insights_high_severity_repeat_threshold` times
    (recurring high-severity condition). A source IP is flagged the same
    way once its attempts reach the bruteforce attempts threshold or it
    tries at least `log_search_ui_insights_bruteforce_users_threshold`
    distinct usernames.
  - **Likely noise** — a non-auth pattern at Informational/Debug
    severity, or a non-auth, non-error pattern occurring at most
    `log_search_ui_insights_low_count_threshold` times.
  - **Review** — everything else: doesn't clearly match a known-noisy or
    known-serious shape, so it's worth a human glance rather than an
    automatic call either way. Every top-source-IP row that isn't
    flagged "Genuine?" is "Review" (there's no "noise" bucket for source
    IPs — every row there represents at least one real failed login).

  **Sudo activity uses its own three-way rule**, since sudo use is
  expected admin behaviour by design rather than something inherently
  suspicious — "Genuine?" here means "worth a second look if it wasn't
  you", not "looks malicious":
  - **Genuine?** — the invoking user became `root` *and* ran a command
    matching `log_search_ui_insights_sudo_sensitive_commands_pattern`
    (account/auth changes, service/firewall control, package changes,
    destructive file operations); or the same (user, target, command)
    combination repeated at least
    `log_search_ui_insights_sudo_repeat_threshold` times.
  - **Likely noise** — a non-root sudo command occurring at most
    `log_search_ui_insights_low_count_threshold` times.
  - **Review** — everything else (e.g. a low-volume `root` command that
    isn't on the sensitive list).

  **Su activity uses a simpler two-threshold rule**, since `su` has no
  per-command detail to check for sensitivity — the tradeoff is that a
  single `su` to `root` is never automatically "noise", since it grants
  a full open-ended root shell rather than one audited command:
  - **Genuine?** — the invoking user became `root` and the same (invoking
    user, target user) pair repeated at least
    `log_search_ui_insights_su_repeat_threshold` times.
  - **Review** — the invoking user became `root` but below that repeat
    threshold (always at least "Review", never "noise"); or a non-root
    `su` above the low-count threshold below.
  - **Likely noise** — a non-root `su` occurring at most
    `log_search_ui_insights_low_count_threshold` times.

**Important caveat, also shown on the page itself**: these labels are
simple heuristics based on count, severity, and pattern matching — they
are *not* a substitute for manual review or a real SIEM/IDS. Treat
"Genuine?" as "worth a closer look", not a confirmed incident, and treat
"Likely noise" as "probably fine", not a guarantee. Always verify
anything that matters before acting on it.

All Insights SQL uses the same bound-parameter (`%s`) and `html.escape()`
discipline as the search page — see "Security notes" below, which covers
both pages.

### Charts

Two pure-SVG charts sit above the tables — no charting library, no new
dependency, just Python string-building (same stdlib-only rule as the
rest of this app):

- **Alert volume over time** — a *stacked*, severity-coloured bar chart
  of row counts per time bucket across the selected window (5-minute
  buckets for `1h`, hourly for `24h`, daily for `7d`/`30d`), so both a
  volume spike *and* what it's actually made of are visible at a glance
  before you even look at the tables below it. Each bar splits into one
  coloured segment per severity level present in that bucket, using the
  same fixed colour-per-severity scheme as the donut chart below (red
  for Emergency down to grey for Debug), with the most severe colour
  always drawn on top so it's the first thing you notice — a bar that's
  all blue/grey (Informational/Debug) reads very differently from one
  with a red/orange cap even at the same total height. Hover any
  segment to see that severity's exact count plus the bucket's total
  (e.g. "18:00: 28 total - Critical: 12") instead of just a bare row
  count. A legend under the chart lists only the severities actually
  present in the window. Buckets with zero rows still render (as an
  empty gap) so the timeline stays continuous.
- **Severity breakdown donut** — the same counts as the Severity
  breakdown table just rendered as a ring chart with a legend, using a
  fixed colour per severity (red for Emergency down to grey for Debug)
  so the same colour always means the same severity across visits.

Both charts are read-only visualisations of the exact same query results
already used to build the KPI cards / tables on the page — no additional
user input is accepted, so there's nothing new to validate beyond what
the existing Insights queries already validate (the time window).

### Insights page performance

Loading `/insights` runs about 10 independent, read-only queries (the
KPI cards, severity breakdown, top-patterns/IPs/sudo/su tables, volume
trend chart, and suppressed-patterns lookups) — none of them depend on
any other's result. They run concurrently, each on its own short-lived
database connection (`_fetch_isolated()` / `INSIGHTS_QUERY_WORKERS` in
`app.py`), instead of one after another on a single shared connection —
so the page's load time is roughly bounded by the slowest single query
rather than the sum of all of them. The 4 KPI numbers themselves (SSH
auth failures, sudo commands, password failures, distinct failed-SSH
source IPs) are also computed by one query using `COUNT(*) FILTER
(WHERE ...)` per condition rather than 4 separate queries, halving the
round trips the KPI cards need (2 total: current window + previous
window for the trend delta, instead of 8). Neither change alters any
number, table, or chart shown on the page — both were checked to
return byte-for-byte identical HTML across several window/host-filter
combinations before being adopted. If `/insights` is still slow after
an upgrade to a version with this change, the next place to look is
`log_search_ui_insights_scan_rows` (default `50000`) — the top-patterns/
sudo/su queries each scan up to that many recent rows and run several
`regexp_replace()` calls per row to normalise messages for grouping,
which is the most CPU-heavy part of the page; lowering it trades away
how far back "top repeated alerts" grouping can see for a faster load.

### Suppressing known-noisy patterns

Once you've reviewed a repeated-alert pattern in the "Top repeated /
similar alerts" table and confirmed it's benign (e.g. a health-check
probe that logs a harmless line every minute), you can dismiss it so it
stops resurfacing every time you check the page — without deleting or
filtering the underlying log data itself, and without needing to
remember it was already reviewed.

- Each row in **Top repeated / similar alerts** has a small "Dismiss"
  form in a new "Actions" column: an optional free-text note (e.g. "known
  monitoring probe, reviewed 2026-09") and two buttons:
  - **Dismiss** records that exact `(service, pattern)` combination as
    suppressed. Use this for a genuinely single, stable message.
  - **Dismiss all** suppresses every pattern from that service,
    regardless of wording — stored as `(service, "")`, an empty-pattern
    sentinel that matches any pattern from the same service. Use this
    when the same noisy service keeps resurfacing as a "different" row
    because the message text varies message to message (a classic case
    is a repeating traceback, where each stack frame line — different
    file/line/function each time — otherwise groups as its own separate
    "repeated pattern" row; dismissing one leaves the others visible).
  Either way, submitting reloads the page with the affected row(s) now
  hidden.
- A note directly above the table ("N pattern(s) hidden — see Suppressed
  patterns below") appears whenever one or more patterns are currently
  suppressed, so it's never silently unclear that rows are missing.
- A persistent "Suppressed / dismissed items (N)" link near the top of
  the page (right below the report-download links) always jumps straight
  to the Suppressed patterns section and always shows the true total
  count, regardless of whether the current window/host filter happens to
  show any hidden rows right now - so there is always a one-click way to
  review or un-suppress something, even if you dismissed it while looking
  at a different window or host filter than the one you're on now.
- A **Suppressed patterns** section further down the page (reachable via
  the note above or the persistent top-of-page link) lists every
  currently-suppressed pattern: service, pattern, your note, who
  suppressed it, and when — each with an "Un-suppress" button that
  removes it from the list and lets it resurface in Top repeated /
  similar alerts again on the next load. A row dismissed via "Dismiss
  all" shows its pattern as `(all patterns from this service)` instead
  of one exact wording, since it isn't tied to a single pattern — but it
  also gets a "Show N pattern(s) currently hidden in this window"
  expandable list, built from the same scan used for Top repeated /
  similar alerts, so "Dismiss all" is never a black box: you can always
  see exactly which pattern text (and how often each occurs) is
  currently being hidden by that wildcard, or a clear "No patterns from
  this service appear in the current window" message if none currently
  match.
- The **Suppressed patterns** section is collapsed by default (a native
  `<details>`/`<summary>` disclosure, no JavaScript) so the page doesn't
  keep getting longer as more patterns get reviewed and dismissed over
  time — the `<summary>` always shows the live count (e.g. "Suppressed
  patterns (23)"), and clicking it expands the table. Even when expanded,
  the table itself is height-capped (~420px) with its own scrollbar, so a
  long-lived lab or fleet with dozens of suppressed patterns never turns
  this into a giant scroll-forever page — you scroll inside the box
  instead. Jumping to the `#suppressed-patterns` anchor (from the note or
  the persistent top-of-page link) still auto-expands the closed
  `<details>` in modern browsers, so both existing links keep working
  exactly as before.
- Submitting Dismiss/Dismiss all/Un-suppress reloads back at the **top**
  of the page (not auto-scrolled to the Suppressed patterns section),
  so you land back where the Top repeated / similar alerts table is,
  with the persistent "Suppressed / dismissed items (N)" link right
  there if you want to jump down and review.
- Suppression is **global, not per-window or per-host** — dismissing a
  pattern hides it from Top repeated / similar alerts regardless of which
  time window or host filter you're viewing, since a benign pattern is
  normally benign everywhere, not just in the window you happened to be
  looking at when you reviewed it. If a pattern later starts behaving
  differently (e.g. spikes far beyond its usual noisy volume), un-suppress
  it and re-review with fresh eyes.
- Implemented as two new POST-only routes, `/insights/suppress` and
  `/insights/unsuppress`, backed by a new database table
  (`log_search_ui_suppressed_patterns_table`, created by `roles/log_db`
  — see that role's README) with a `UNIQUE (service, pattern)`
  constraint, so re-dismissing an already-suppressed pattern just
  refreshes its note/timestamp rather than erroring or duplicating. Both
  routes read the submitted username from the same Basic Auth header
  already used for login (purely as an audit label in `suppressed_by` —
  it is not re-validated as a fresh authorization check beyond the
  standard `_check_basic_auth()` gate every request already passes
  through) and redirect back to `/insights` (302/303 POST-redirect-GET)
  with the same window/host you were viewing, so reloading the result
  page never re-submits the form. There is no session/cookie state
  involved (matching the rest of this stateless, Basic-Auth-only app),
  so this is intentionally a plain POST form rather than a
  token-protected one — acceptable here because every request already
  requires the same credential this whole tool is gated behind.

### Downloadable reports

A "Download report" line above the KPI cards offers two separate CSV
exports, both scoped to the currently-selected time window and host
filter (`/insights/export.csv?window=<window>&report=<management|ops>`,
plus `&host=<host>` automatically appended whenever a host filter is
active on the page):

- **Management summary (CSV)** — a short, skim-in-under-a-minute export:
  the four KPI counts, the severity breakdown, and an overall assessment
  (total log lines, high-severity line count). No per-alert or per-IP
  detail — intended for someone who just wants the headline numbers.
- **Full ops-detail report (CSV)** — everything in the management summary
  plus the full Top repeated/similar alerts, Top source IPs, Top sudo
  activity, and Top su activity tables (counts, severities/sample
  commands, genuine/review/noise assessment and rationale, and a relative
  drill-down URL for each row) — intended for the Linux operations team
  to work from directly.

Both are built with Python's stdlib `csv` module as a flat, multi-section
CSV (blank rows and a section-title row separate each table — a common
convention for flat-file multi-table reports, since a plain CSV can't
represent multiple sheets the way a spreadsheet workbook can). The
`report` query parameter is validated against a fixed two-value allow
list and silently falls back to `management` on anything else — the same
tolerant-validation style already used by `window`/`priority` elsewhere
in this app. Values in the CSV come only from the database or from this
app's own fixed labels, never from unescaped request input.

### Drilling down into the actual log lines

Every number on the Insights page links back to the Search page (or, for
the distinct-IP and sudo-commands counts, jumps to the Top source IPs /
Top sudo activity tables further down the same page) so you're never
stuck staring at a count with no way to see what's behind it. There is
no KPI card for `su` (see "Top su activity" above), but its table has
the same per-row drill-down links described below.

- Of the four **KPI cards**, "SSH auth failures" and "Password failures"
  are clickable and open Search pre-filtered to the exact same window and
  criteria used to compute that count (e.g. "SSH auth failures" opens
  Search with `tag=sshd`, the failure regex, and `since` set to the start
  of the window). "Distinct source IPs" and "Sudo commands run" instead
  scroll down to the Top source IPs / Top sudo activity tables, since
  those tables are already the per-IP or per-user/command breakdown
  behind those counts.
- Each row of **Top repeated / similar alerts** has a "View log lines"
  link that reconstructs a regex from the normalized pattern (reversing
  the `<IP>`/`<PORT>`/`<PID>`/`<UID>`/`<USER>` placeholders back into their
  original matching shapes) and opens Search filtered to exactly the raw
  rows that were grouped into that row.
- Each row of **Top source IPs** has a "View log lines" link that opens
  Search filtered to that specific IP, `tag=sshd`, and the same window.
- Each row of **Top sudo activity** has a "View log lines" link built
  from a regex combining that row's exact invoking user, target
  ("becoming") user, and base command, filtered to `tag=sudo` and the
  same window.
- Each row of **Top su activity** has a "View log lines" link built from
  a regex combining that row's exact invoking user and target
  ("becoming") user, anchored to `su`'s own `pam_unix(su:` /
  `pam_unix(su-l:` PAM service string so it can never match sudo's own
  `pam_unix(sudo:session): session opened...` line (which uses the same
  "session opened for user X by Y" wording) even though the Search
  page's `tag=su` filter alone would loosely match both.
- Each row of **Severity breakdown** has a "View log lines" link that
  opens Search filtered to that exact severity level and window (e.g.
  "Error" → `priority=3`). The Search page's own "Severity" pulldown
  (new alongside the existing message/host/tag/date filters) drives this
  — pick any level there directly, or land on it pre-selected via one of
  these links.

These links only ever populate the existing Search page's own `q`,
`mode`, `tag`, `since`, and `priority` fields — no new query path or
bypass of the usual bound-parameter search logic. A severity value
outside 0–7 (or anything non-numeric) is silently ignored as "no
filter" rather than erroring, the same tolerant handling every other
Search filter already gets.

Every drill-down link also stamps on the Insights time-window key you
were viewing (e.g. `window=1h`), purely so Search can show a "← Back to
Insights" link right above the search form that returns you to that
same window — not the default one. This back link only appears when the
`window` value is a real, recognized window key; visiting Search
directly, or with a hand-edited/garbage `window` value, shows no back
link at all. Paging through drill-down results with Next/Previous keeps
the back link working throughout.

## Security notes

- **SQL injection**: every user-supplied value is passed as a bound
  psycopg2 parameter (`%s`), never string-interpolated into SQL. The only
  interpolated identifier (the table name) comes from the Ansible-managed
  `config.py`, not from request input.
- **XSS**: every dynamic value written into the HTML response — form
  field echoes and log content alike — is passed through Python's
  `html.escape()` before being inserted, so request input or log message
  content cannot inject markup or script.
- **Least privilege**: the app connects using `log_db_reader_user` (the
  SELECT-only role created by `roles/log_db`) — it cannot modify or
  delete log data even if compromised. It also runs as its own
  unprivileged system account, with `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, and `PrivateTmp` set in its systemd unit.
- The default `log_db_reader_password` is a placeholder — see
  `roles/log_db/README.md`'s "Securing credentials" section; both roles
  share the same underlying `log_db_*` variables via vault.

## Authentication

This UI sits directly on the open port (`log_search_ui_port`, default
`8081`) with **no reverse proxy** in front of it - instead, HTTP Basic
Auth is checked directly inside `app.py` itself (`_check_basic_auth()`,
right at the top of the WSGI `application()` entry point), using only
the Python standard library (`base64` + `hmac.compare_digest()` for a
timing-safe comparison) - no httpd, no mod_wsgi, no Flask-Login, no
session/cookie state. Every request (including `/insights` and
`/insights/export.csv`) re-sends and re-checks credentials.

- Controlled by `log_search_ui_auth_enabled` (default `true`). When
  `true`, every request without a valid `Authorization: Basic ...`
  header gets a `401 Unauthorized` with a `WWW-Authenticate: Basic`
  challenge header - the browser then prompts for a username/password
  itself, no login page needed.
- The username/password are `log_search_ui_auth_user` /
  `log_search_ui_auth_password`. By default these fall back to the
  shared `dashboard_auth_user` / `dashboard_auth_password` variables
  defined in `group_vars/all/dashboard_auth.yml` - **one credential
  protects both this UI and `roles/linuxdb_dashboard`** by design.
  Override `log_search_ui_auth_user`/`_password` directly instead if
  you want this UI to use a different credential than
  `linuxdb_dashboard`.
- **Change the password before using this outside a throwaway lab** -
  vault it the same way as the existing `log_db_*` passwords:

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
- To reset the password later, just change `dashboard_auth_password`
  (or the role-specific override) and re-run the playbook - `config.py`
  is re-templated and the service restarts via the existing
  `notify: Restart log-search-ui` handler, same as any other config
  change.

### Kerberos authentication mode

Instead of the local username/password above, you can validate each
login against a real Kerberos realm - FreeIPA/IdM or Active Directory,
both are just Kerberos KDCs under the hood, so the same code path works
against either. The browser still shows the same Basic Auth
username/password prompt - nothing changes for the end user - but
`app.py` verifies the submitted credentials with a throwaway `kinit`
instead of comparing against a locally-stored password.

- Set `log_search_ui_auth_mode` (or the shared `dashboard_auth_mode` in
  `group_vars/all/dashboard_auth.yml`) to `kerberos`. Default is
  `local` - the existing behaviour above, completely unchanged.
- Set `log_search_ui_auth_kerberos_realm` (or the shared
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
- **Optional group gating**: set `log_search_ui_auth_kerberos_required_group`
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
- `log_search_ui_auth_user`/`_password` (the local-mode credential) are
  simply ignored while `log_search_ui_auth_mode` is `kerberos`.

**Before enabling this against a real environment, confirm:**

1. The host is domain-joined - check with `realm list` or
   `sssctl domain-list`.
2. A manual `kinit <user>@<realm>` already succeeds from that host
   today (proves KDC reachability and `/etc/krb5.conf` realm mapping).
3. `id <user>` resolves cleanly via SSSD (proves NSS/group lookups
   work) - and, if using group gating, that the group name you plan to
   use in `log_search_ui_auth_kerberos_required_group` is correct
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
port (`log_search_ui_port`), no new port, no firewalld change, no
httpd/mod_ssl, no reverse proxy. The UI serves plain `https://` instead
of `http://` on that same port once TLS is enabled.

- Controlled by `log_search_ui_tls_enabled` (default `true`).
- **Self-signed certificate, generated automatically on first
  deploy** if `log_search_ui_tls_generate_self_signed` is `true`
  (default) and no cert already exists at `log_search_ui_tls_cert_path`
  - a single `openssl req -x509` command task guarded by `creates:`,
  so it only ever runs once per host and is left alone (idempotent) on
  every later run. No `community.crypto` collection dependency -
  `requirements.yml` stays unchanged.
- Cert/key paths default to
  `{{ log_search_ui_app_dir }}/tls/log-search-ui.{crt,key}`
  (`log_search_ui_tls_cert_path` / `_key_path`). The private key is
  always `0600`, owned by `log_search_ui_user`.
- The self-signed cert's Common Name defaults to the target host's own
  FQDN (`log_search_ui_tls_cn`, `ansible_fqdn` fact) and is valid for
  `log_search_ui_tls_cert_days` (default `3650` days).
- Because the cert is self-signed, browsers and `curl` will warn about
  an untrusted certificate (`curl -k` / "Advanced -> Proceed" in a
  browser) unless you import it as a trusted cert yourself, or supply
  your own CA-signed cert/key at `log_search_ui_tls_cert_path` /
  `_key_path` before the first deploy (the role only generates one if
  the file doesn't already exist - it never overwrites a cert you
  provide).
- If you ever need a fresh cert (e.g. it expired, or the hostname
  changed), delete the file at `log_search_ui_tls_cert_path` and
  re-run the playbook - a new one is generated the same way.

## Why app.py isn't templated

`app.py` builds its HTML with plain Python string formatting, including
a literal CSS block full of `{ }` braces. If it were deployed with
`ansible.builtin.template`, Ansible's Jinja engine would try to resolve
those braces as Ansible variables and corrupt the file. To avoid that
collision, `app.py` is deployed verbatim with `ansible.builtin.copy`, and
only the small, brace-free `config.py` carries Ansible-templated values
(database host/name/table/user/password, max results).

## Removing httpd (if you deployed the earlier version of this role)

An earlier version of this role deployed an httpd + mod_wsgi variant of
this same app, alongside a second copy served by the stdlib WSGI server
above (for side-by-side comparison), under the name
`log-search-ui-standalone.service`. That comparison variant has been
removed — this role now deploys **only** the stdlib server, as
`log-search-ui.service`.

Re-running this role against a host that had the old version
automatically removes the old httpd vhost config and the old
`-standalone`-named service unit/app directory (see `tasks/main.yml`).
It does **not** automatically uninstall the `httpd` and
`python3-mod_wsgi` packages themselves, unset the
`httpd_can_network_connect_db` SELinux boolean, or close the old
`8080/tcp` firewalld port — those are left for you to remove by hand,
in case `httpd` is used for something else on the host. On a host
dedicated to this role (e.g. `admin1`), that's usually safe to do:

```bash
# Stop and disable httpd, remove the vhost + WSGI entry point it used
sudo systemctl stop httpd
sudo systemctl disable httpd
sudo rm -f /etc/httpd/conf.d/log-search-ui.conf

# Uninstall httpd + mod_wsgi if nothing else on this host needs them
sudo dnf remove httpd python3-mod_wsgi

# Undo the SELinux boolean set for httpd's DB access, if SELinux is enabled
sudo setsebool -P httpd_can_network_connect_db off

# Close the old httpd port in firewalld (log_search_ui_port, the new
# service, uses 8081 - confirm that before closing 8080)
sudo firewall-cmd --permanent --remove-port=8080/tcp
sudo firewall-cmd --reload
```

Then confirm the stdlib service is the only one running:

```bash
systemctl status log-search-ui
journalctl -u log-search-ui -f
```

## Variables

### defaults/main.yml

- `log_search_ui_port` (int, default `8081`) — port the service
  listens on.
- `log_search_ui_app_dir` (string, default `/opt/log_search_ui`) —
  install directory.
- `log_search_ui_user` (string, default `log_search_ui`) — unprivileged
  system account the service runs as, created by this role.
- `log_search_ui_max_results` (int, default `500`) — default/first-tier
  page size; the "Results per page" pulldown also offers its 2x/4x/10x
  multiples.
- `log_search_ui_max_results_all` (int, default `20000`) — hard cap
  when the pulldown's "All" option is selected (fetches everything
  matching in one page, no Previous/Next).
- `log_search_ui_db_host` / `log_search_ui_db_name` /
  `log_search_ui_db_table` / `log_search_ui_db_user` /
  `log_search_ui_db_password` — read-only DB connection. Default to the
  matching `log_db_*` / `rsyslog_collector_db_host` variables so all three
  roles (`log_db`, `rsyslog_collector`, `log_search_ui`) stay in sync
  automatically as long as you only override the `log_db_*` versions.
- `log_search_ui_insights_default_window` (string, default `"24h"`) —
  time window (`1h`/`24h`/`7d`/`30d`) shown by default on `/insights`.
- `log_search_ui_insights_scan_rows` (int, default `50000`) — hard cap
  on rows scanned by the "Top repeated / similar alerts" query,
  regardless of window/table size.
- `log_search_ui_insights_top_alerts` (int, default `15`) — rows shown
  in the "Top repeated / similar alerts" table.
- `log_search_ui_insights_top_ips` (int, default `10`) — rows shown in
  the "Top source IPs" table.
- `log_search_ui_insights_bruteforce_attempts_threshold` (int, default
  `10`) — occurrence/attempt count at which a pattern or source IP is
  flagged "Genuine?".
- `log_search_ui_insights_bruteforce_users_threshold` (int, default
  `3`) — distinct usernames tried by one source IP at which it's
  flagged "Genuine?" (credential-stuffing shape).
- `log_search_ui_insights_repeat_ip_threshold` (int, default `3`) —
  distinct source IPs behind one auth-failure pattern at which it's
  flagged "Genuine?" (distributed attempt shape).
- `log_search_ui_insights_high_severity_repeat_threshold` (int, default
  `5`) — recurrence count at which a non-auth Error-or-worse pattern is
  flagged "Genuine?".
- `log_search_ui_insights_low_count_threshold` (int, default `2`) —
  recurrence count at or below which a non-auth, non-error pattern is
  flagged "Likely noise".
- `log_search_ui_suppressed_patterns_table` (string, default falls back
  to `log_db_suppressed_patterns_table`, `log_search_ui_suppressed_patterns`)
  — table used by the "Suppress/dismiss known-noisy patterns" feature
  above; created and granted by `roles/log_db` (see that role's README).
- `log_search_ui_auth_enabled` (bool, default `true`) — require HTTP
  Basic Auth on every request — see "Authentication" above.
- `log_search_ui_auth_user` / `log_search_ui_auth_password` — Basic
  Auth credential; falls back to the shared `dashboard_auth_user` /
  `dashboard_auth_password` (default `admin` /
  `CHANGE_ME_dashboard_admin`) — vault the password, see
  "Authentication" above.
- `log_search_ui_auth_mode` (string, default falls back to shared
  `dashboard_auth_mode`, `local`) — `local` (password above) or
  `kerberos` — see "Kerberos authentication mode" above.
- `log_search_ui_auth_kerberos_realm` (string, default falls back to
  shared `dashboard_auth_kerberos_realm`, `""`) — Kerberos realm to
  authenticate against when auth mode is `kerberos`. Required in that
  mode.
- `log_search_ui_auth_kerberos_required_group` (string, default falls
  back to shared `dashboard_auth_kerberos_required_group`, `""`) —
  optional POSIX/AD group required for access in `kerberos` mode. Empty
  = any valid principal.
- `log_search_ui_auth_kerberos_kinit_path` / `_kdestroy_path` /
  `_getent_path` / `_id_path` (string, defaults `/usr/bin/kinit` /
  `/usr/bin/kdestroy` / `/usr/bin/getent` / `/usr/bin/id`) — paths to
  the Kerberos/NSS tools used in `kerberos` mode.
- `log_search_ui_tls_enabled` (bool, default `true`) — serve HTTPS
  instead of HTTP on `log_search_ui_port` — see "TLS" above.
- `log_search_ui_tls_generate_self_signed` (bool, default `true`) —
  auto-generate a self-signed cert on first deploy if one doesn't
  already exist.
- `log_search_ui_tls_cn` (string, default target host's `ansible_fqdn`)
  — Common Name on the generated self-signed cert.
- `log_search_ui_tls_cert_days` (int, default `3650`) — validity
  period (days) of the generated self-signed cert.
- `log_search_ui_tls_cert_path` / `log_search_ui_tls_key_path` (string,
  default `{{ log_search_ui_app_dir }}/tls/log-search-ui.crt` /
  `.key`) — paths to the TLS certificate and private key (key is mode
  `0600`).

This role also reads `dashboard_auth_user` and `dashboard_auth_password`
from `group_vars/all/dashboard_auth.yml` (shared with
`roles/linuxdb_dashboard`) unless overridden as above.

## Usage

```yaml
- name: Configure the log search web front end
  hosts: log_collector
  become: true
  roles:
    - role: log_search_ui
```

Then browse to `https://<collector-host>:8081/` to search, or
`https://<collector-host>:8081/insights` for the data insights page
(see "The Insights page" above) — TLS is on by default (see "TLS"
above; `curl -k` or "proceed anyway" in a browser is expected for the
self-signed cert) — and log in with the Basic Auth credentials from
`group_vars/all/dashboard_auth.yml` (see "Authentication" above). See
the top-level `README.md`'s "Pulling and redeploying updates" section
for how to pick up a change like this on an already-deployed host.
