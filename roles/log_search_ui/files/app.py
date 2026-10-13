"""
Managed by Ansible (roles/log_search_ui) - do not edit by hand on target
hosts (edit roles/log_search_ui/files/app.py in the repo instead).

Minimal front end for the log_db database populated by
roles/rsyslog_collector. Two pages, both served from this one module:

- Search (/)         - search log messages by pattern (plain substring or
                        POSIX regex), host, tag, and date range, instead
                        of grepping flat files across the fleet.
- Insights (/insights) - heuristic data insights over a chosen time
                        window: authentication-failure / sudo / password-
                        failure counts, a severity breakdown, the most
                        repeated/similar alerts, and the top source IPs
                        behind failed SSH logins - each with a rough
                        genuine/review/noise assessment. See the
                        "Insights" section of roles/log_search_ui/README.md
                        for the exact heuristics and their caveats.

Deliberately dependency-free: this is a plain WSGI application using only
the Python standard library (html, re, datetime, urllib.parse) plus
psycopg2 - no Flask/Werkzeug/Jinja2. Flask itself is not available in
RHEL 9's own repositories (only via EPEL), and this repo's package
choices are constrained to RHEL BaseOS/AppStream so the whole stack stays
dnf-update maintainable with no third-party repos. Served by Python's
own stdlib wsgiref.simple_server (see wsgi_standalone_server.py), which
calls the `application` callable below directly - no framework, no
httpd, no mod_wsgi required in between.

Security notes:
- All query values are passed as psycopg2 bound parameters (%s) - never
  interpolated into SQL strings - so user input cannot inject SQL. Table
  names are substituted via a plain string .replace() of a __TABLE__
  placeholder (not user-controlled - it only ever comes from config.py,
  which is templated by Ansible from a trusted variable), never from
  request input.
- Every dynamic value written into the HTML response is passed through
  html.escape() before being inserted, so user input (or log content)
  cannot inject markup/script.
- The database connection uses log_search's SELECT-only role (see
  roles/log_db) - this application cannot modify or delete log data even
  if compromised.
- The Insights page's "genuine/review/noise" labels are simple heuristics
  (count/severity/pattern-matching thresholds) computed in Python on
  small, already-fetched result sets - not a security product. Treat
  them as a triage hint, not a verdict.
"""

import base64
import concurrent.futures
import csv
import hmac
import io
import math
import os
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from html import escape
from urllib.parse import parse_qs, urlencode

import psycopg2
import psycopg2.extras

import config

# ---------------------------------------------------------------------------
# Shared design system
#
# These tokens (and the .tabs/.tab/.card-style/.badge/status colour
# choices below) are deliberately kept identical to the ones in
# roles/linuxdb_dashboard/files/app.py's PAGE_CSS - the two apps are
# separate, independently-deployed WSGI services (no shared static
# assets, see the module docstring above), but sharing one visual
# language across both makes them read as a single connected
# application. If you change a colour/spacing token here for a real
# design reason, make the matching change over there too.
# ---------------------------------------------------------------------------

PAGE_CSS = """
:root {
  --bg: #f4f6f9;
  --surface: #ffffff;
  --surface-alt: #f7f8fa;
  --border: #d7dbe1;
  --border-strong: #c3c9d1;
  --text: #1a1d21;
  --text-muted: #5b6067;
  --text-faint: #8a8f96;
  --accent: #1a56c4;
  --accent-hover: #123f92;
  --accent-soft: #e7eefb;
  --radius: 8px;
  --radius-sm: 6px;
  --shadow-card: 0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.06);
  --success: #1c7a3c;
  --success-bg: #e2f5e8;
  --info: #1a5c96;
  --info-bg: #e3f0fb;
  --warn: #966615;
  --warn-bg: #fbf0da;
  --crit: #a3352f;
  --crit-bg: #fbe4e2;
  --mono: Consolas, "Courier New", "Liberation Mono", monospace;
}
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; background: var(--bg); color: var(--text); }
header { background: var(--surface); padding: 16px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
header h1 { margin: 0; font-size: 1.25rem; }
header h1 a { color: var(--text); text-decoration: none; }
/* Cross-app switcher (LinuxDB Dashboard / Log Search / Insights / Guide,
   same fixed order as linuxdb_dashboard's own header) reuses the
   .tabs/.tab pill styling below, just without the body's
   margin-bottom - it sits inline in the header's right-hand side. */
.tabs.app-switcher { margin: 0; }
main { padding: 24px 32px; max-width: 1400px; margin: 0 auto; }
h2 { margin-top: 1.8em; margin-bottom: 0.6em; font-size: 1.05rem; }
.subtitle { color: var(--text-muted); margin-top: 0; margin-bottom: 1.2em; }
.tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }
.tab { padding: 8px 16px; border-radius: var(--radius-sm); background: var(--surface); border: 1px solid var(--border); text-decoration: none; color: var(--text); font-weight: 600; font-size: 0.9rem; }
.tab.active { background: var(--accent); border-color: var(--accent); color: #fff; }
.tab:hover:not(.active) { border-color: var(--border-strong); }
form { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 1.1em 1.5em; margin-bottom: 1.5em; box-shadow: var(--shadow-card); }
/* .search-form (main filter form) and .window-form (Insights filter bar)
   get the same soft accent tint + left accent bar as LinuxDB Dashboard's
   "Jump to a host" box - keeps the "this is a quick-action search/filter
   control" visual language consistent across all three apps. */
.search-form, .window-form { background: var(--accent-soft); border-left: 4px solid var(--accent); }
.window-form { padding: 0.9em 1.2em; display: flex; align-items: flex-end; flex-wrap: wrap; gap: 0.8em; }
.window-form label { margin-bottom: 0; }
.row { display: flex; flex-wrap: wrap; gap: 1em; margin-bottom: 0.8em; }
.field { flex: 1 1 220px; }
label { display: block; font-size: 0.82em; color: var(--text-muted); margin-bottom: 0.3em; font-weight: 600; }
input[type=text], input[type=datetime-local], select { width: 100%; padding: 0.5em 0.6em; box-sizing: border-box; border: 1px solid var(--border); border-radius: var(--radius-sm); font-size: 0.92em; background: #fff; color: var(--text); }
input:focus, select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
button { padding: 0.55em 1.3em; border: none; border-radius: var(--radius-sm); background: var(--accent); color: #fff; cursor: pointer; font-weight: 600; font-size: 0.92em; }
button:hover { background: var(--accent-hover); }
table { width: 100%; border-collapse: collapse; background: var(--surface); margin-bottom: 1em; border-radius: var(--radius); overflow: hidden; }
th, td { text-align: left; padding: 0.55em 0.7em; border-bottom: 1px solid var(--border); font-size: 0.88em; vertical-align: top; }
th { background: var(--surface-alt); color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 0.76em; letter-spacing: 0.03em; }
tr:hover td { background: var(--surface-alt); }
.msg { font-family: var(--mono); white-space: pre-wrap; word-break: break-word; }
.meta { color: var(--text-muted); font-size: 0.85em; margin-bottom: 0.8em; }
.pagination { display: flex; gap: 1em; margin-top: 0.6em; }
.pagination a { color: var(--accent); text-decoration: none; font-weight: 600; }
.pagination a:hover { text-decoration: underline; }
.back-link { margin-bottom: 1em; }
.back-link a { color: var(--accent); text-decoration: none; font-size: 0.9em; font-weight: 600; }
.back-link a:hover { text-decoration: underline; }
.error { color: var(--crit); background: var(--crit-bg); border: 1px solid #f0b8b4; padding: 0.7em 1em; border-radius: var(--radius-sm); margin-bottom: 1em; }
.kpi-grid { display: flex; flex-wrap: wrap; gap: 1em; margin-bottom: 1.8em; }
.kpi-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow-card); padding: 1.1em 1.4em; flex: 1 1 220px; display: block; text-decoration: none; color: inherit; transition: box-shadow 0.15s, border-color 0.15s, transform 0.15s; }
.kpi-card:hover { border-color: var(--accent); box-shadow: 0 4px 14px rgba(26,86,196,0.16); transform: translateY(-1px); }
.kpi-card .value { font-size: 2em; font-weight: 700; line-height: 1.1; font-variant-numeric: tabular-nums; }
.kpi-card .label { color: var(--text-muted); font-size: 0.85em; margin-top: 0.3em; }
.drill { font-size: 0.85em; white-space: nowrap; }
.drill a { color: var(--accent); font-weight: 600; text-decoration: none; }
.drill a:hover { text-decoration: underline; }
.badge { display: inline-block; padding: 0.2em 0.7em; border-radius: 12px; font-size: 0.78em; font-weight: 700; white-space: nowrap; }
.badge-genuine { background: var(--crit-bg); color: var(--crit); }
.badge-review { background: var(--warn-bg); color: var(--warn); }
.badge-noise { background: var(--success-bg); color: var(--success); }
.rationale { color: var(--text-muted); font-size: 0.8em; margin-top: 0.35em; }
.caveat { color: var(--text-muted); font-size: 0.85em; border-top: 1px solid var(--border); margin-top: 2em; padding-top: 1em; }
.report-links { margin: -0.4em 0 1.2em; font-size: 0.9em; }
.report-links a { color: var(--accent); text-decoration: none; font-weight: 600; }
.report-links a:hover { text-decoration: underline; }
.trend-chart { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow-card); padding: 1.2em 1.3em 0.6em; margin-bottom: 1.8em; overflow-x: auto; }
.trend-chart svg { display: block; min-width: 320px; }
.trend-legend { display: flex; flex-wrap: wrap; gap: 0.5em 1.4em; font-size: 0.8em; margin: 0.8em 0 0.3em; color: var(--text-muted); }
.severity-flex { display: flex; flex-wrap: wrap; gap: 1.6em; align-items: stretch; margin-bottom: 1.4em; }
.donut-wrap { display: flex; align-items: center; gap: 1.6em; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow-card); padding: 1.2em 1.5em; flex: 0 0 auto; }
.legend { display: flex; flex-direction: column; gap: 0.45em; font-size: 0.83em; }
.legend-item { display: flex; align-items: center; gap: 0.55em; white-space: nowrap; }
.legend-item .legend-count { color: var(--text-muted); font-variant-numeric: tabular-nums; margin-left: auto; padding-left: 0.8em; }
.legend-swatch { width: 11px; height: 11px; border-radius: 3px; display: inline-block; flex: 0 0 auto; }
.severity-table-col { flex: 1 1 380px; min-width: 320px; }
.kpi-card .delta { font-size: 0.78em; margin-top: 0.4em; font-weight: 600; }
.delta-up { color: var(--crit); }
.delta-down { color: var(--success); }
.delta-flat { color: var(--text-faint); font-weight: 400; }
.window-form .field-inline { display: flex; flex-direction: column; flex: 0 0 220px; gap: 0.25em; }
.window-form .field-inline label { margin-bottom: 0; font-size: 0.8em; }
.window-form .field-inline select, .window-form .field-inline input[type=text] { width: 100%; box-sizing: border-box; }
.suppress-form { display: flex; flex-direction: column; align-items: flex-start; gap: 0.3em; min-width: 110px; max-width: 150px; }
.suppress-form input[type=text] { width: 100%; box-sizing: border-box; padding: 0.2em 0.45em; font-size: 0.78em; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.suppress-form button { width: 100%; padding: 0.2em 0.6em; font-size: 0.78em; border-radius: var(--radius-sm); }
.table-scroll { overflow-x: auto; margin-bottom: 1em; }
.table-scroll table { margin-bottom: 0; }
.suppress-btn { background: var(--surface-alt); color: var(--text); border: 1px solid var(--border); }
.suppress-btn:hover { background: #eef0f3; }
.suppress-btn-all { background: var(--warn-bg); color: var(--warn); border: 1px solid #ecd9ad; }
.suppress-btn-all:hover { background: #f7e6b8; }
.unsuppress-btn { background: var(--success); }
.unsuppress-btn:hover { background: #156130; }
.suppressed-section { margin-top: 2.2em; }
.suppressed-note { color: var(--text-muted); font-size: 0.85em; }
/* Suppressed patterns grows by one row every time something new gets
   dismissed, so it is collapsed by default (summary keeps the live count
   visible) and the table itself is height-capped with its own scrollbar -
   the page no longer gets longer as more patterns accumulate. Jumping to
   the #suppressed-patterns anchor above still auto-expands a closed
   <details> in modern browsers, so that link keeps working unchanged. */
.suppressed-details { margin-top: 0.5em; }
.suppressed-details > summary { cursor: pointer; font-size: 1.1em; font-weight: 700; margin-bottom: 0.6em; list-style: none; }
.suppressed-details > summary::-webkit-details-marker { display: none; }
.suppressed-details > summary::before { content: "\\25B8\\00A0\\00A0"; }
.suppressed-details[open] > summary::before { content: "\\25BE\\00A0\\00A0"; }
.suppressed-scroll { max-height: 420px; overflow-y: auto; }
.suppressed-scroll table th { position: sticky; top: 0; background: var(--surface-alt); }
.hidden-patterns { margin-top: 0.5em; font-size: 0.85em; }
.hidden-patterns summary { cursor: pointer; color: var(--text); font-weight: 600; }
.hidden-patterns ul { margin: 0.5em 0 0 1.2em; padding: 0; font-family: var(--mono); }
.hidden-patterns li { margin-bottom: 0.35em; word-break: break-word; }
.hidden-count { color: var(--text-muted); }
.hidden-patterns-empty { margin: 0.4em 0 0 0; }
"""


def page_head(active):
    """Full <head> + <header> HTML for one page, with the cross-app
    switcher (LinuxDB Dashboard / Log Search / Insights / Package
    Search / Guide, same fixed order on every page in both apps - see
    render_nav()) embedded directly in the header next to the "Log
    Search" heading - same header layout as linuxdb_dashboard's
    PAGE_HEAD / GUIDE_PAGE_HEAD + _app_switcher_html, rather than the
    switcher living as a separate tab row further down inside <main>.

    `active` is "search" or "insights" - whichever page this is gets
    rendered active (highlighted, non-navigating-away) instead of
    linked, same meaning render_nav()'s `active` argument always had.
    """
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <title>Log Search</title>\n'
        "  <style>" + PAGE_CSS + "</style>\n</head>\n<body>\n"
        '  <header><h1><a href="/">Log Search</a></h1>' + render_nav(active) + "</header>\n<main>\n"
    )


PAGE_TAIL = "</main>\n</body>\n</html>\n"


# ---------------------------------------------------------------------------
# Small shared helpers (query-string parsing, nav bar)
# ---------------------------------------------------------------------------


def _first(qs, name, default=""):
    return qs.get(name, [default])[0].strip()


def render_nav(active):
    """Cross-app navigation switcher embedded in the header of every Log
    Search / Insights page (see page_head()).

    Item order and labels are kept IDENTICAL to linuxdb_dashboard's own
    _app_switcher_html(), on purpose: LinuxDB Dashboard, Log Search,
    Insights, Package Search, Guide - always in that order, always with
    those exact labels, regardless of which app happens to be rendering
    the header. Previously this app listed its own two tabs first
    ("Search", "Insights", then the LinuxDB Dashboard sibling link,
    then Guide), while linuxdb_dashboard listed LinuxDB Dashboard first
    - so the same destination sat in a different horizontal slot (and
    "Search" vs "Log Search" used different labels for the same link)
    depending on which app you were currently looking at, making the
    nav bar appear to reshuffle itself as you clicked between apps.
    Search and Insights are always present (both live in this app, at
    fixed slots 2 and 3). LinuxDB Dashboard, Package Search and Guide
    are added at slots 1, 4 and 5, navigating in the same tab, whenever
    the sibling app / packages / guide URLs are configured (see
    defaults/main.yml) - this stays additive: an unconfigured deployment
    just shows Log Search and Insights, exactly as before, just without
    the now-empty slots either side. Package Search's position never
    moves - it always sits right after Insights and right before Guide,
    whether or not it (or Guide) is actually configured.
    """
    def cls(name):
        return "tab active" if name == active else "tab"

    items = []

    sibling_name = getattr(config, "SIBLING_APP_NAME", "")
    sibling_url = getattr(config, "SIBLING_APP_URL", "")
    if sibling_name and sibling_url:
        items.append(
            '<a class="tab" href="{url}">{name}</a>'.format(
                url=escape(sibling_url), name=escape(sibling_name)
            )
        )

    items.append('<a class="{0}" href="/">Log Search</a>'.format(cls("search")))
    items.append('<a class="{0}" href="/insights">Insights</a>'.format(cls("insights")))

    packages_url = getattr(config, "PACKAGES_APP_URL", "")
    if packages_url:
        items.append(
            '<a class="tab" href="{url}">Package Search</a>'.format(url=escape(packages_url))
        )

    guide_url = getattr(config, "GUIDE_APP_URL", "")
    if guide_url:
        items.append(
            '<a class="tab" href="{url}">Guide</a>'.format(url=escape(guide_url))
        )

    return '<nav class="tabs app-switcher">' + "".join(items) + "</nav>"


def get_connection():
    return psycopg2.connect(
        host=config.DB_HOST,
        dbname=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        connect_timeout=5,
    )


def _fetch_isolated(fetch_fn, *args, **kwargs):
    """Runs one fetch_*(cur, ...) function on its own short-lived
    connection instead of a cursor shared with other queries. Used by
    render_insights_page() to run its ~10 independent queries
    concurrently (see INSIGHTS_QUERY_WORKERS) rather than serially on
    one connection - each of psycopg2's own connections is only safe
    to use from the thread that owns it, never shared across threads,
    so parallel fetches each need their own connection. Any exception
    here propagates up through the Future's .result() call exactly as
    it would from a plain sequential call, so the existing
    'Insights failed to load' error handling in render_insights_page()
    is unchanged."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            return fetch_fn(cur, *args, **kwargs)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Search page
# ---------------------------------------------------------------------------


def page_size_choices():
    """Pulldown tiers: the configured default plus 2x/4x/10x of it, so
    changing log_search_ui_max_results rescales the whole list."""
    base = config.MAX_RESULTS
    return sorted({base, base * 2, base * 4, base * 10})


def parse_page_size(raw, default):
    """Returns 'all' or a positive int page size."""
    raw = (raw or "").strip().lower()
    if raw == "all":
        return "all"
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def parse_priority(raw):
    """Validates a user-supplied severity value: returns an int 0-7, or
    None if missing/out of range/not an integer at all (silently ignored
    as 'no filter' rather than erroring - matches the tolerant handling
    of every other Search filter on this page)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 <= value <= 7 else None


def build_page_link(params, page_size, offset, window=None):
    """URL (query string only) for a Previous/Next link that keeps every
    current filter and page size, changing only the offset. `window`, if
    given, is carried along purely so the "Back to Insights" link above
    the results keeps working after paging - it plays no part in the
    actual search filtering."""
    query = {}
    for key in ("q", "mode", "host", "tag", "since", "until", "priority"):
        if params[key]:
            query[key] = params[key]
    query["page_size"] = str(page_size)
    query["offset"] = str(offset)
    if window:
        query["window"] = window
    return "?" + urlencode(query)


def render_form(params):
    def selected(value):
        return "selected" if params["mode"] == value else ""

    def page_size_selected(value):
        return "selected" if params["page_size"] == str(value) else ""

    page_size_option_html = "".join(
        f'<option value="{tier}" {page_size_selected(tier)}>{tier} per page</option>'
        for tier in page_size_choices()
    )
    all_selected = "selected" if params["page_size"] == "all" else ""

    priority_selected = parse_priority(params["priority"])
    priority_option_html = "".join(
        '<option value="{value}" {sel}>{name}</option>'.format(
            value=level,
            sel="selected" if priority_selected == level else "",
            name=escape("{0} ({1})".format(SEVERITY_NAMES[level], level)),
        )
        for level in sorted(SEVERITY_NAMES)
    )

    return f"""
  <form method="get" class="search-form">
    <div class="row">
      <div class="field">
        <label for="q">Message contains / matches</label>
        <input type="text" id="q" name="q" value="{escape(params['q'])}" placeholder="e.g. authentication failure">
      </div>
      <div class="field" style="flex: 0 0 160px;">
        <label for="mode">Match mode</label>
        <select id="mode" name="mode">
          <option value="contains" {selected('contains')}>Contains (fast)</option>
          <option value="regex" {selected('regex')}>Regex (POSIX)</option>
        </select>
      </div>
    </div>
    <div class="row">
      <div class="field">
        <label for="host">Host (fromhost)</label>
        <input type="text" id="host" name="host" value="{escape(params['host'])}" placeholder="e.g. idm1.lab.example.com">
      </div>
      <div class="field">
        <label for="tag">Tag (syslogtag)</label>
        <input type="text" id="tag" name="tag" value="{escape(params['tag'])}" placeholder="e.g. sshd">
      </div>
      <div class="field" style="flex: 0 0 200px;">
        <label for="priority">Severity</label>
        <select id="priority" name="priority">
          <option value="">Any</option>
          {priority_option_html}
        </select>
      </div>
    </div>
    <div class="row">
      <div class="field">
        <label for="since">Since</label>
        <input type="datetime-local" id="since" name="since" value="{escape(params['since'])}">
      </div>
      <div class="field">
        <label for="until">Until</label>
        <input type="datetime-local" id="until" name="until" value="{escape(params['until'])}">
      </div>
      <div class="field" style="flex: 0 0 200px;">
        <label for="page_size">Results per page</label>
        <select id="page_size" name="page_size" onchange="this.form.submit()">
          {page_size_option_html}
          <option value="all" {all_selected}>All (up to {config.MAX_RESULTS_ALL})</option>
        </select>
      </div>
    </div>
    <button type="submit">Search</button>
  </form>
"""


def render_results(rows, page_size, offset, has_more, params, max_results_all, window=None):
    all_mode = page_size == "all"
    nav = ""

    if all_mode:
        if has_more:
            note = (
                f" (showing first {len(rows)} row(s), capped at "
                f"max_results_all = {max_results_all} \u2014 narrow your search to see the rest)"
            )
        else:
            note = f" (showing all matching rows)"
    else:
        start = offset + 1 if rows else offset
        end = offset + len(rows)
        note = f" (rows {start}\u2013{end} on this page)"
        nav_links = []
        if offset > 0:
            prev_offset = max(0, offset - page_size)
            nav_links.append(f'<a href="{escape(build_page_link(params, page_size, prev_offset, window))}">\u00ab Previous</a>')
        if has_more:
            next_offset = offset + page_size
            nav_links.append(f'<a href="{escape(build_page_link(params, page_size, next_offset, window))}">Next \u00bb</a>')
        if nav_links:
            nav = f'  <p class="pagination">{" ".join(nav_links)}</p>'

    lines = [f'  <p class="meta">{len(rows)} row(s){note}</p>', "  <table>"]
    lines.append("    <tr><th>Received</th><th>Host</th><th>Tag</th><th>Message</th></tr>")
    for row in rows:
        lines.append(
            "    <tr><td>{received}</td><td>{host}</td><td>{tag}</td>"
            '<td class="msg">{message}</td></tr>'.format(
                received=escape(str(row["receivedat"])),
                host=escape(row["fromhost"] or ""),
                tag=escape(row["syslogtag"] or ""),
                message=escape(row["message"] or ""),
            )
        )
    lines.append("  </table>")
    if nav:
        lines.append(nav)
    return "\n".join(lines)


def run_search(params, page_size, offset):
    """Returns (rows, error, has_more)."""
    conditions = []
    values = []

    if params["q"]:
        if params["mode"] == "regex":
            conditions.append("message ~* %s")
            values.append(params["q"])
        else:
            conditions.append("message ILIKE %s")
            values.append("%" + params["q"] + "%")

    if params["host"]:
        conditions.append("fromhost ILIKE %s")
        values.append("%" + params["host"] + "%")

    if params["tag"]:
        conditions.append("syslogtag ILIKE %s")
        values.append("%" + params["tag"] + "%")

    if params["since"]:
        conditions.append("receivedat >= %s")
        values.append(params["since"])

    if params["until"]:
        conditions.append("receivedat <= %s")
        values.append(params["until"])

    priority = parse_priority(params["priority"])
    if priority is not None:
        conditions.append("priority = %s")
        values.append(priority)

    where_clause = " AND ".join(conditions) if conditions else "TRUE"

    if page_size == "all":
        fetch_limit = config.MAX_RESULTS_ALL
        fetch_offset = 0
    else:
        fetch_limit = page_size
        fetch_offset = offset

    query = (
        "SELECT receivedat, fromhost, syslogtag, message FROM "
        + config.DB_TABLE
        + " WHERE " + where_clause
        + " ORDER BY receivedat DESC LIMIT %s OFFSET %s"
    )
    # Fetch one extra row beyond the page size purely to detect whether
    # more rows exist (drives the Next link / "capped" notice) - it's
    # dropped below and never shown.
    values.append(fetch_limit + 1)
    values.append(fetch_offset)

    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, values)
                rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - surface a friendly message, log detail server-side
        # Errors go to stderr, which systemd captures directly into the
        # journal (see roles/log_search_ui/files/wsgi_standalone_server.py) -
        # no separate log file to go check.
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return [], (
            "Search failed - check the application's error output for details "
            "('journalctl -u log-search-ui')."
        ), False

    has_more = len(rows) > fetch_limit
    if has_more:
        rows = rows[:fetch_limit]
    return rows, None, has_more


def render_search_page(qs):
    params = {
        "q": _first(qs, "q"),
        "mode": _first(qs, "mode", "contains"),
        "host": _first(qs, "host"),
        "tag": _first(qs, "tag"),
        "since": _first(qs, "since"),
        "until": _first(qs, "until"),
        "priority": _first(qs, "priority"),
        "page_size": _first(qs, "page_size", str(config.MAX_RESULTS)),
    }

    page_size = parse_page_size(params["page_size"], config.MAX_RESULTS)
    params["page_size"] = str(page_size)  # normalize (e.g. bad input) before re-displaying the form

    if page_size == "all":
        offset = 0
    else:
        try:
            offset = max(0, int(_first(qs, "offset", "0")))
        except ValueError:
            offset = 0

    searched = any(params.values())
    subtitle = f'  <p class="subtitle">Searches {escape(config.DB_TABLE)} instead of grepping flat files across the fleet.</p>'
    body = [page_head("search"), subtitle]

    # Present only when we arrived here via a drill-down link from
    # Insights (which stamps its current time-window key onto every
    # link it builds) - an invalid/unrecognized value is treated the
    # same as absent, so a stray or hand-edited ?window=... never shows
    # a broken/misleading back link.
    back_window = _first(qs, "window")
    if back_window not in WINDOW_CHOICES:
        back_window = ""
    if back_window:
        back_link = "/insights?" + urlencode({"window": back_window})
        body.append(f'  <p class="back-link">\u2190 <a href="{escape(back_link)}">Back to Insights</a></p>')

    error = None
    if searched:
        rows, error, has_more = run_search(params, page_size, offset)
        if error:
            body.append(f'  <div class="error">{escape(error)}</div>')

    body.append(render_form(params))

    if searched and not error:
        body.append(render_results(rows, page_size, offset, has_more, params, config.MAX_RESULTS_ALL, back_window))

    body.append(PAGE_TAIL)
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Insights page
#
# Heuristic data insights over a chosen time window: authentication
# failures, sudo activity, password failures, a severity breakdown, the
# most repeated/similar alerts, and the top source IPs behind failed SSH
# logins - each with a rough genuine/review/noise assessment computed in
# Python (never in SQL) from small already-fetched result sets. See the
# "Insights" section of README.md for the exact thresholds and caveats.
# ---------------------------------------------------------------------------

SSH_FAILURE_PATTERN = r"failed password|invalid user|authentication failure|too many authentication failures"
PASSWORD_FAILURE_PATTERN = r"failed password|authentication failure|incorrect password|FAILED su for"
SOURCE_IP_CAPTURE_PATTERN = r"from ((?:[0-9]{1,3}\.){3}[0-9]{1,3})"
USERNAME_CAPTURE_PATTERN = r"for (?:invalid user )?([^ ]+) from"
SSH_HAS_IP_PATTERN = r"from [0-9]{1,3}(\.[0-9]{1,3}){3}"

# sudo's own log line shape (message text after the "sudo:" syslogtag),
# e.g. "alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/usr/bin/
# systemctl restart httpd" - captures who ran sudo, which account they
# became, and the command verbatim. Only ever applied to rows already
# filtered to syslogtag ILIKE 'sudo%' AND message ILIKE '%COMMAND=%' (the
# same filter behind the existing "Sudo commands run" KPI), so PAM
# session-open/close lines from the same 'sudo:' tag (which have neither
# field) never reach these patterns.
SUDO_INVOKING_USER_CAPTURE_PATTERN = r"^\s*(\S+)\s*:"
SUDO_TARGET_USER_CAPTURE_PATTERN = r"USER=(\S+)"
SUDO_COMMAND_CAPTURE_PATTERN = r"COMMAND=(.+)$"

# su's own PAM session line, e.g. "pam_unix(su-l:session): session opened
# for user root(uid=0) by alice(uid=1000)". Unlike sudo, su has no COMMAND=
# equivalent - it just opens an interactive shell as the target user, so
# there's no per-command field to extract here, only who invoked su and
# who they became. Only ever applied to rows already filtered to a
# syslogtag that is genuinely "su" (not "sudo" - see SU_TAG_* below) AND
# message ILIKE '%session opened%', so su's own "session closed" lines
# and su auth-FAILURE lines (already covered by the existing password-
# failures KPI) never reach these patterns.
SU_INVOKING_USER_CAPTURE_PATTERN = r"by ([^\s(]+)"
SU_TARGET_USER_CAPTURE_PATTERN = r"for user ([^\s(]+)"

# Anchor used only in drill-down links (not in the aggregation query
# above, which already excludes sudo precisely via its syslogtag
# filter). The general search page's own "tag" filter is a plain
# substring ILIKE '%tag%' match, and "su" is a substring of "sudo" -
# so a drill-down search for tag="su" would ALSO surface sudo's own
# "pam_unix(sudo:session): session opened for user X by Y" line, which
# uses the exact same "session opened for user X by Y" wording as su's.
# Requiring "pam_unix(su:" or "pam_unix(su-" (su-l) immediately - never
# "pam_unix(sudo" - in the drill-down regex itself closes that gap.
SU_MESSAGE_ANCHOR_PATTERN = r"pam_unix\(su[:-]"

AUTH_FAILURE_RE = re.compile(SSH_FAILURE_PATTERN + "|" + PASSWORD_FAILURE_PATTERN, re.I)

SEVERITY_NAMES = {
    0: "Emergency", 1: "Alert", 2: "Critical", 3: "Error",
    4: "Warning", 5: "Notice", 6: "Informational", 7: "Debug",
}

# Ordered so the window pulldown renders smallest-to-largest.
WINDOW_CHOICES = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

WINDOW_LABELS = {
    "1h": "the last hour",
    "24h": "the last 24 hours",
    "7d": "the last 7 days",
    "30d": "the last 30 days",
}

# Bucket width (seconds) for the "Alert volume over time" chart - chosen
# per window so the trend chart always renders a sensible, readable
# number of bars (12 for 1h, 24 for 24h, 7 for 7d, 30 for 30d).
WINDOW_BUCKET_SECONDS = {
    "1h": 300,       # 5-minute buckets
    "24h": 3600,     # hourly buckets
    "7d": 86400,     # daily buckets
    "30d": 86400,    # daily buckets
}

# Bucket label format per window - matches the granularity above.
WINDOW_BUCKET_LABEL_FMT = {
    "1h": "%H:%M",
    "24h": "%H:%M",
    "7d": "%m-%d",
    "30d": "%m-%d",
}

# How many of the Insights page's independent DB queries to run
# concurrently (see _fetch_isolated() and render_insights_page()).
# There are 10 independent fetches per page load; 10 lets every one
# of them start at once rather than queueing behind each other. Each
# concurrent fetch opens its own short-lived connection, so this also
# bounds how many simultaneous connections one Insights page load can
# open against Postgres (well under the default max_connections=100).
INSIGHTS_QUERY_WORKERS = 10

# Consistent colour ramp for severity levels across the donut chart, the
# volume trend chart (not currently split by severity, but kept together
# for a single source of truth), and any future severity-coloured UI.
# Emergency (0) is the most alarming red, running warm-to-cool through
# Warning/Notice, landing on the app's own accent blue for Informational
# (6) and a neutral grey for Debug (7) - ties the chart palette to the
# same --accent used for buttons/links elsewhere on the page instead of
# an unrelated hue.
SEVERITY_COLORS = {
    0: "#7a1330", 1: "#c1272d", 2: "#d9531e", 3: "#e6890f",
    4: "#d4b106", 5: "#2e7d32", 6: "#1a56c4", 7: "#8a8f96",
}

# btrim(message) below (not just plain 'message') matters beyond cosmetics:
# rsyslog's traditional format always leaves one leading space in the
# extracted 'message' field (the space after the 'tag:' separator), so an
# un-trimmed 'pattern' here would carry that leading space on every row.
# suppress_pattern()/the /insights/suppress handler strip() the submitted
# pattern before storing it (see application() below), so an un-trimmed
# pattern here would never byte-for-byte match what got stored - single
# 'Dismiss' would insert correctly but the row would silently reappear on
# every reload since the exact-match lookup would never succeed. Trimming
# once here keeps every consumer (display, hidden form field, suppression
# lookup) working from the same normalized text.
TOP_PATTERNS_QUERY = r"""
WITH scanned AS (
    SELECT syslogtag, priority, fromhost, receivedat, message
    FROM __TABLE__
    WHERE receivedat >= %s __HOST_FILTER__
    ORDER BY receivedat DESC
    LIMIT %s
),
normalized AS (
    SELECT
        regexp_replace(syslogtag, '\[[0-9]+\]', '', 'g') AS service,
        priority,
        fromhost,
        receivedat,
        message,
        substring(message from 'from ((?:[0-9]{1,3}\.){3}[0-9]{1,3})') AS source_ip,
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(btrim(message), '[0-9]{1,3}(\.[0-9]{1,3}){3}', '<IP>', 'g'),
                        'port [0-9]+', 'port <PORT>', 'g'
                    ),
                    '\[[0-9]+\]', '[<PID>]', 'g'
                ),
                'uid=[0-9]+', 'uid=<UID>', 'g'
            ),
            'for (invalid user )?[^ ]+ from', 'for <USER> from', 'g'
        ) AS pattern
    FROM scanned
)
SELECT
    pattern,
    service,
    MIN(priority) AS min_priority,
    COUNT(*) AS cnt,
    COUNT(DISTINCT fromhost) AS distinct_hosts,
    COUNT(DISTINCT source_ip) AS distinct_source_ips,
    MIN(receivedat) AS first_seen,
    MAX(receivedat) AS last_seen,
    (ARRAY_AGG(message ORDER BY receivedat DESC))[1] AS sample_message
FROM normalized
GROUP BY pattern, service
ORDER BY cnt DESC
LIMIT %s
"""

TOP_SSH_SOURCE_IPS_QUERY = r"""
SELECT
    substring(message from %s) AS source_ip,
    COUNT(*) AS attempts,
    COUNT(DISTINCT substring(message from %s)) AS distinct_users,
    COUNT(DISTINCT fromhost) AS distinct_targets,
    MIN(receivedat) AS first_seen,
    MAX(receivedat) AS last_seen
FROM __TABLE__
WHERE receivedat >= %s __HOST_FILTER__
  AND syslogtag ILIKE %s
  AND message ~* %s
  AND message ~* %s
GROUP BY source_ip
ORDER BY attempts DESC
LIMIT %s
"""

# Who ran sudo, which account they became, and what they ran - grouped by
# (invoking user, target user, executable) rather than the full command
# line, so e.g. "systemctl restart httpd" / "systemctl restart nginx" by
# the same person collapse into one "systemctl" row instead of two
# singletons (same normalize-then-group idea as TOP_PATTERNS_QUERY above,
# just splitting on the first whitespace instead of a regex placeholder
# swap). scan_cap bounds this the same way INSIGHTS_SCAN_ROWS already
# bounds the top-patterns scan - this extracts three fields per row so
# it's worth the same safety cap on a big table/window.
TOP_SUDO_ACTIVITY_QUERY = r"""
WITH scanned AS (
    SELECT message, fromhost, receivedat
    FROM __TABLE__
    WHERE receivedat >= %s __HOST_FILTER__
      AND syslogtag ILIKE %s
      AND message ILIKE %s
    ORDER BY receivedat DESC
    LIMIT %s
),
extracted AS (
    SELECT
        COALESCE(substring(message from %s), '(unknown)') AS invoking_user,
        COALESCE(substring(message from %s), '(unknown)') AS target_user,
        substring(message from %s) AS full_command,
        fromhost,
        receivedat
    FROM scanned
)
SELECT
    invoking_user,
    target_user,
    split_part(full_command, ' ', 1) AS base_command,
    COUNT(*) AS cnt,
    COUNT(DISTINCT fromhost) AS distinct_hosts,
    MIN(receivedat) AS first_seen,
    MAX(receivedat) AS last_seen,
    (ARRAY_AGG(full_command ORDER BY receivedat DESC))[1] AS sample_command
FROM extracted
WHERE full_command IS NOT NULL
GROUP BY invoking_user, target_user, base_command
ORDER BY cnt DESC
LIMIT %s
"""

# The tag filter here is deliberately two ILIKE clauses rather than one
# simple prefix match: syslogtag ILIKE 'su%' alone would also match
# "sudo:"/"sudo[1234]:" (sudo's tag starts with the same two letters),
# and sudo has its own PAM "session opened for user X by Y" line too -
# so a loose 'su%' match combined with the session-opened filter below
# would silently double-count sudo's PAM session events as su activity.
# 'su:%' covers the no-PID tag form, 'su[%' covers the PID-bracket form
# ("su[6789]:") - neither matches anything starting "sudo".
TOP_SU_ACTIVITY_QUERY = r"""
WITH scanned AS (
    SELECT message, fromhost, receivedat
    FROM __TABLE__
    WHERE receivedat >= %s __HOST_FILTER__
      AND (syslogtag ILIKE %s OR syslogtag ILIKE %s)
      AND message ILIKE %s
    ORDER BY receivedat DESC
    LIMIT %s
),
extracted AS (
    SELECT
        COALESCE(substring(message from %s), '(unknown)') AS invoking_user,
        COALESCE(substring(message from %s), '(unknown)') AS target_user,
        fromhost,
        receivedat
    FROM scanned
)
SELECT
    invoking_user,
    target_user,
    COUNT(*) AS cnt,
    COUNT(DISTINCT fromhost) AS distinct_hosts,
    MIN(receivedat) AS first_seen,
    MAX(receivedat) AS last_seen
FROM extracted
GROUP BY invoking_user, target_user
ORDER BY cnt DESC
LIMIT %s
"""


def _sql(template, table, host_filter=""):
    # .replace() rather than .format()/% so the regex literals above
    # (which contain { } of their own, e.g. {1,3}) are never mistaken
    # for format placeholders. table always comes from config.py
    # (Ansible-templated, not request input). __HOST_FILTER__ is a
    # fixed SQL fragment built only by _host_filter_fragment() below
    # (never raw request input) - the actual host value itself is
    # always passed as a separate bound %s parameter, never spliced
    # into this string.
    return template.replace("__TABLE__", table).replace("__HOST_FILTER__", host_filter)


def _host_filter_fragment(host):
    """Returns (sql_fragment, params) for the optional per-host scoping
    filter added to every Insights query (see the "Per-host scoping"
    section of README.md). Empty/blank host means fleet-wide - the
    exact same 'no filter at all' behaviour Insights always had before
    this feature existed. Matches fromhost the same tolerant way the
    Search page's own host filter already does (substring,
    case-insensitive) - not an exact match - so "idm1" still finds
    "idm1.lab.example.com"."""
    host = (host or "").strip()
    if not host:
        return "", []
    return " AND fromhost ILIKE %s", ["%" + host + "%"]


def _base_where(since, until=None, host=None):
    """Returns (sql_fragment, params) for the since/until/host filters
    shared by every Insights KPI/severity-breakdown query - always
    starts with 'receivedat >= %s', meant to be combined with further
    AND-ed conditions specific to each query. `until`, if given, adds
    an exclusive upper bound (used only for the previous-period side of
    the KPI trend-delta comparison - see fetch_kpis())."""
    clause = "receivedat >= %s"
    params = [since]
    if until is not None:
        clause += " AND receivedat < %s"
        params.append(until)
    host_clause, host_params = _host_filter_fragment(host)
    return clause + host_clause, params + host_params


def parse_window(raw, default):
    raw = (raw or "").strip().lower()
    return raw if raw in WINDOW_CHOICES else default


def fetch_kpis(cur, table, since, until=None, host=None):
    """until (exclusive upper bound) and host (substring filter on
    fromhost) are both optional - see _base_where(). until is used
    only for the previous-period side of the KPI trend-delta
    comparison (render_insights_page() calls this twice: once for the
    current window with until=None, once for the immediately-preceding
    window of the same length with an explicit until).

    All 4 KPI numbers come from one query using FILTER (WHERE ...) per
    aggregate - each COUNT only counts rows matching its own condition,
    same as if it were 4 separate queries with 4 separate WHERE clauses,
    but computed in a single pass over the table instead of 4. This
    used to be 4 round trips per call (8 total across the current +
    previous period calls render_insights_page() makes); merging them
    was verified to return byte-identical results across current/
    previous window and host-filtered/unfiltered scenarios before
    replacing the old per-condition queries (see git history for the
    standalone comparison script this was checked against)."""
    base_clause, base_params = _base_where(since, until, host)
    cur.execute(
        _sql(
            "SELECT "
            "COUNT(*) FILTER (WHERE syslogtag ILIKE %s AND message ~* %s), "
            "COUNT(*) FILTER (WHERE syslogtag ILIKE %s AND message ILIKE %s), "
            "COUNT(*) FILTER (WHERE message ~* %s), "
            "COUNT(DISTINCT substring(message from %s)) FILTER (WHERE syslogtag ILIKE %s AND message ~* %s) "
            "FROM __TABLE__ WHERE " + base_clause,
            table,
        ),
        ["sshd%", SSH_FAILURE_PATTERN, "sudo%", "%COMMAND=%", PASSWORD_FAILURE_PATTERN,
         SOURCE_IP_CAPTURE_PATTERN, "sshd%", SSH_FAILURE_PATTERN] + base_params,
    )
    row = cur.fetchone()
    return {
        "ssh_auth_failures": row[0],
        "sudo_commands": row[1],
        "password_failures": row[2],
        "distinct_failed_ssh_ips": row[3],
    }


def fetch_severity_breakdown(cur, table, since, host=None):
    base_clause, base_params = _base_where(since, host=host)
    cur.execute(
        _sql("SELECT priority, COUNT(*) FROM __TABLE__ WHERE " + base_clause + " GROUP BY priority ORDER BY priority", table),
        base_params,
    )
    return cur.fetchall()


def fetch_top_patterns(cur, table, since, scan_cap, top_n, host=None):
    host_filter, host_params = _host_filter_fragment(host)
    cur.execute(_sql(TOP_PATTERNS_QUERY, table, host_filter), [since] + host_params + [scan_cap, top_n])
    return cur.fetchall()


def fetch_top_source_ips(cur, table, since, top_n, host=None):
    host_filter, host_params = _host_filter_fragment(host)
    cur.execute(
        _sql(TOP_SSH_SOURCE_IPS_QUERY, table, host_filter),
        [SOURCE_IP_CAPTURE_PATTERN, USERNAME_CAPTURE_PATTERN, since] + host_params
        + ["sshd%", SSH_FAILURE_PATTERN, SSH_HAS_IP_PATTERN, top_n],
    )
    return cur.fetchall()


def fetch_top_sudo_activity(cur, table, since, scan_cap, top_n, host=None):
    host_filter, host_params = _host_filter_fragment(host)
    cur.execute(
        _sql(TOP_SUDO_ACTIVITY_QUERY, table, host_filter),
        [since] + host_params + [
            "sudo%", "%COMMAND=%", scan_cap,
            SUDO_INVOKING_USER_CAPTURE_PATTERN, SUDO_TARGET_USER_CAPTURE_PATTERN, SUDO_COMMAND_CAPTURE_PATTERN,
            top_n,
        ],
    )
    return cur.fetchall()


def fetch_top_su_activity(cur, table, since, scan_cap, top_n, host=None):
    host_filter, host_params = _host_filter_fragment(host)
    cur.execute(
        _sql(TOP_SU_ACTIVITY_QUERY, table, host_filter),
        [since] + host_params + [
            "su:%", "su[%", "%session opened%", scan_cap,
            SU_INVOKING_USER_CAPTURE_PATTERN, SU_TARGET_USER_CAPTURE_PATTERN,
            top_n,
        ],
    )
    return cur.fetchall()


# SQL fragments for each fixed bucket width, expressed purely in terms of
# date_trunc()/extract() on the "timestamp without time zone" column
# itself - deliberately never round-tripping through to_timestamp()/epoch
# (which would reinterpret the naive value against the session's
# TimeZone GUC and could silently shift bucket boundaries by whatever
# that offset is). bucket_seconds only ever comes from our own
# WINDOW_BUCKET_SECONDS dict, never from request input, so this lookup
# is as safe as every other fixed, non-user-controlled SQL fragment in
# this file (e.g. __TABLE__ substitution above).
_BUCKET_SQL_EXPR = {
    300: "date_trunc('hour', receivedat) + floor(extract(minute FROM receivedat) / 5) * interval '5 minutes'",
    3600: "date_trunc('hour', receivedat)",
    86400: "date_trunc('day', receivedat)",
}


def fetch_volume_trend(cur, table, since, bucket_seconds, host=None):
    """Row counts per time bucket *per severity level*, for the "Alert
    volume over time" chart - grouping by priority as well as bucket
    lets the chart show a stacked, severity-coloured bar per bucket
    instead of just an undifferentiated row count, so a spike's actual
    composition (e.g. a burst of Critical/Error rows vs. routine Debug
    noise) is visible at a glance. host, if given, scopes the whole
    chart to just that one host - see _host_filter_fragment().

    Returns a dict of {(bucket_start_datetime, priority): count}
    covering only the (bucket, priority) combinations that actually had
    rows - render_volume_trend_chart() fills in the zero-count gaps
    (using the exact same flooring logic, see _floor_to_bucket()) so
    the chart still shows a continuous timeline.
    """
    bucket_expr = _BUCKET_SQL_EXPR.get(bucket_seconds, _BUCKET_SQL_EXPR[3600])
    base_clause, base_params = _base_where(since, host=host)
    cur.execute(
        _sql(
            "SELECT " + bucket_expr + " AS bucket, priority, COUNT(*) "
            "FROM __TABLE__ WHERE " + base_clause + " GROUP BY bucket, priority ORDER BY bucket",
            table,
        ),
        base_params,
    )
    return {(bucket, priority): count for bucket, priority, count in cur.fetchall()}


# ---------------------------------------------------------------------------
# Suppressed ("acknowledged") repeated-alert patterns - see README
# "Suppressing known-noisy patterns". Backed by a small dedicated table
# (config.SUPPRESSED_PATTERNS_TABLE, created by roles/log_db - see that
# role's tasks/main.yml) that the reader DB role has narrow
# INSERT/SELECT/DELETE privileges on, while keeping its access to the
# main log table itself SELECT-only (least privilege).
# ---------------------------------------------------------------------------


def fetch_suppressed_patterns(cur):
    """Returns the currently-suppressed set as {(service, pattern), ...}
    for O(1) membership checks in render_top_patterns_table()."""
    cur.execute(_sql("SELECT service, pattern FROM __TABLE__", config.SUPPRESSED_PATTERNS_TABLE))
    return {(service, pattern) for service, pattern in cur.fetchall()}


def fetch_suppressed_patterns_full(cur):
    """Full rows (service, pattern, note, suppressed_by, suppressed_at)
    for the "Suppressed patterns" management sub-view, newest first."""
    cur.execute(
        _sql(
            "SELECT service, pattern, note, suppressed_by, suppressed_at FROM __TABLE__ ORDER BY suppressed_at DESC",
            config.SUPPRESSED_PATTERNS_TABLE,
        )
    )
    return cur.fetchall()


def suppress_pattern(cur, service, pattern, note, suppressed_by):
    """Idempotent - re-dismissing an already-suppressed (service, pattern)
    just refreshes note/suppressed_by/suppressed_at (ON CONFLICT), it
    doesn't error or create a duplicate row (see the UNIQUE (service,
    pattern) constraint in roles/log_db/tasks/main.yml)."""
    cur.execute(
        _sql(
            "INSERT INTO __TABLE__ (service, pattern, note, suppressed_by) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (service, pattern) DO UPDATE SET note = EXCLUDED.note, "
            "suppressed_by = EXCLUDED.suppressed_by, suppressed_at = now()",
            config.SUPPRESSED_PATTERNS_TABLE,
        ),
        [service, pattern, note or None, suppressed_by or None],
    )


def unsuppress_pattern(cur, service, pattern):
    cur.execute(
        _sql("DELETE FROM __TABLE__ WHERE service = %s AND pattern = %s", config.SUPPRESSED_PATTERNS_TABLE),
        [service, pattern],
    )


def _floor_to_bucket(dt, bucket_seconds):
    """Mirrors _BUCKET_SQL_EXPR in plain Python so the bucket list built
    here for chart continuity lines up exactly with the SQL-side keys."""
    if bucket_seconds == 86400:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket_seconds == 300:
        return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
    return dt.replace(minute=0, second=0, microsecond=0)  # 3600 (and fallback)


def assess_pattern(row, cfg):
    """row: (pattern, service, min_priority, cnt, distinct_hosts,
    distinct_source_ips, first_seen, last_seen, sample_message).
    Returns (verdict, rationale) - verdict is one of
    'genuine' / 'review' / 'noise'. Heuristic only - see module docstring."""
    pattern, service, min_priority, cnt, distinct_hosts, distinct_source_ips, first_seen, last_seen, sample_message = row
    is_auth = bool(AUTH_FAILURE_RE.search(pattern or ""))
    severity = min_priority

    if is_auth and (
        (distinct_source_ips or 0) >= cfg["repeat_ip_threshold"]
        or cnt >= cfg["bruteforce_count_threshold"]
    ):
        detail = "seen {0} times".format(cnt)
        if distinct_source_ips:
            detail += " from {0} distinct source IP(s)".format(distinct_source_ips)
        return ("genuine", "Authentication-failure pattern {0} - review for a possible brute-force attempt.".format(detail))

    if severity is not None and severity <= 3 and cnt >= cfg["high_severity_repeat_threshold"]:
        return (
            "genuine",
            "{0} message recurring {1} times - worth investigating.".format(
                SEVERITY_NAMES.get(severity, "High-severity"), cnt
            ),
        )

    if not is_auth and (severity is None or severity >= 6 or cnt <= cfg["low_count_threshold"]):
        if severity is None or severity >= 6:
            return ("noise", "Informational/debug severity and not an auth-related pattern - looks routine.")
        return ("noise", "Low volume, non-auth pattern - likely routine.")

    return ("review", "Doesn't clearly match a known noisy or known-serious pattern - worth a quick look.")


def assess_ip(row, cfg):
    """row: (source_ip, attempts, distinct_users, distinct_targets,
    first_seen, last_seen). Returns (verdict, rationale)."""
    source_ip, attempts, distinct_users, distinct_targets, first_seen, last_seen = row
    if attempts >= cfg["bruteforce_count_threshold"] or (distinct_users or 0) >= cfg["bruteforce_users_threshold"]:
        return (
            "genuine",
            "{0} failed attempt(s) trying {1} distinct username(s) - looks like a brute-force/credential-guessing attempt.".format(
                attempts, distinct_users
            ),
        )
    return ("review", "{0} failed attempt(s) so far - low volume, keep an eye on it.".format(attempts))


def assess_sudo(row, cfg):
    """row: (invoking_user, target_user, base_command, cnt,
    distinct_hosts, first_seen, last_seen, sample_command). Returns
    (verdict, rationale). Unlike assess_pattern/assess_ip (aimed at
    spotting attacks), sudo activity is expected admin behaviour by
    design - 'genuine?' here just means "privileged enough to warrant a
    second look if it wasn't you", not "looks malicious"."""
    invoking_user, target_user, base_command, cnt, distinct_hosts, first_seen, last_seen, sample_command = row
    is_root = (target_user or "").lower() == "root"
    is_sensitive = bool(cfg["sudo_sensitive_re"].search(sample_command or ""))

    if is_root and is_sensitive:
        return (
            "genuine",
            "{0} became root and ran a sensitive command ({1} time(s)) - confirm this was expected.".format(
                invoking_user, cnt
            ),
        )
    if cnt >= cfg["sudo_repeat_threshold"]:
        return (
            "genuine",
            "{0} ran this command {1} times as {2} - review for automation/misuse if that's unexpected.".format(
                invoking_user, cnt, target_user
            ),
        )
    if not is_root and cnt <= cfg["low_count_threshold"]:
        return ("noise", "Low-volume, non-root sudo use - looks routine.")
    return ("review", "Sudo activity worth a quick look.")


def assess_su(row, cfg):
    """row: (invoking_user, target_user, cnt, distinct_hosts, first_seen,
    last_seen). Returns (verdict, rationale). su has no per-command log
    of its own (see SU_*_CAPTURE_PATTERN above) - the tradeoff for that
    lack of detail is that su grants a full, open-ended interactive shell
    as the target user rather than one audited command, so switching to
    root via su is treated as at least worth a look even as a single
    occurrence (never automatically 'noise'), unlike sudo's more granular
    per-command sensitivity check."""
    invoking_user, target_user, cnt, distinct_hosts, first_seen, last_seen = row
    is_root = (target_user or "").lower() == "root"

    if is_root and cnt >= cfg["su_repeat_threshold"]:
        return (
            "genuine",
            "{0} switched to root via su {1} time(s) - review for automation/misuse if that's unexpected.".format(
                invoking_user, cnt
            ),
        )
    if is_root:
        return (
            "review",
            "{0} switched to root via su - a full open-ended root shell, not a single audited command. "
            "Confirm this was expected.".format(invoking_user),
        )
    if cnt <= cfg["low_count_threshold"]:
        return ("noise", "Low-volume su to a non-root account - looks routine.")
    return ("review", "Su activity worth a quick look.")


def render_badge(verdict):
    label = {"genuine": "Genuine?", "review": "Review", "noise": "Likely noise"}.get(verdict, verdict)
    return '<span class="badge badge-{cls}">{label}</span>'.format(cls=escape(verdict), label=escape(label))


def render_window_form(active_window, host=""):
    """Time-window pulldown plus an optional host filter. The host
    field is a plain text input next to an explicit Apply button rather
    than self-submitting on change (like the window pulldown does) -
    re-submitting on every keystroke would refire the whole Insights
    query set. Leaving the field blank keeps the page fleet-wide,
    exactly as before this feature existed."""
    options = "".join(
        '<option value="{value}" {sel}>{value}</option>'.format(
            value=escape(w), sel="selected" if w == active_window else ""
        )
        for w in WINDOW_CHOICES
    )
    window_field = (
        '    <div class="field-inline">'
        '<label for="window">Time window</label>'
        '<select id="window" name="window" onchange="this.form.submit()">'
        + options +
        "</select>"
        "</div>"
    )
    host_field = (
        '    <div class="field-inline">'
        '<label for="host">Host (optional)</label>'
        '<input type="text" id="host" name="host" value="{host}" placeholder="e.g. idm1.lab.example.com">'
        "</div>"
    ).format(host=escape(host))
    clear_link = ""
    if host:
        clear_link = '<a href="/insights?{qs}">Clear host filter</a>'.format(
            qs=escape(urlencode({"window": active_window}))
        )
    return (
        '  <form method="get" class="window-form">\n'
        "    " + window_field + "\n"
        "    " + host_field + "\n"
        '    <button type="submit">Apply</button>\n'
        "    " + clear_link + "\n"
        "  </form>\n"
    )


def build_drilldown_link(q="", mode="contains", host="", tag="", since=None, priority=None, window=None):
    """Query string (path + query) for a Search-page link that reproduces
    (as closely as the existing Search filters allow) the row set behind
    an Insights count - lets you click a KPI/table row and see the
    actual matching log lines instead of just a number. `window` (the
    Insights time-window key, e.g. "24h") is carried through purely so
    Search can render a "Back to Insights" link that returns to the same
    window you drilled down from - it is never used as a search filter."""
    query = {}
    if q:
        query["q"] = q
    if mode:
        query["mode"] = mode
    if host:
        query["host"] = host
    if tag:
        query["tag"] = tag
    if since is not None:
        query["since"] = since.strftime("%Y-%m-%dT%H:%M")
    if priority is not None:
        query["priority"] = str(priority)
    if window:
        query["window"] = window
    return "/?" + urlencode(query)


# Reverses the message-normalization done in TOP_PATTERNS_QUERY (see
# above) back into a POSIX regex, so a "Top repeated alerts" row can link
# to the exact matching raw log lines on the Search page instead of just
# showing the normalized pattern text.
_PATTERN_PLACEHOLDER_REGEX = {
    "<IP>": r"[0-9]{1,3}(\.[0-9]{1,3}){3}",
    "<PORT>": r"[0-9]+",
    "<PID>": r"[0-9]+",
    "<UID>": r"[0-9]+",
    # normalization collapses an optional "invalid user " prefix into this
    # same token (see 'for (invalid user )?[^ ]+ from' below), so match both
    "<USER>": r"(invalid user )?[^ ]+",
}
_PATTERN_PLACEHOLDER_RE = re.compile(r"(<IP>|<PORT>|<PID>|<UID>|<USER>)")


def pattern_to_regex(pattern):
    parts = _PATTERN_PLACEHOLDER_RE.split(pattern or "")
    out = []
    for part in parts:
        if part in _PATTERN_PLACEHOLDER_REGEX:
            out.append(_PATTERN_PLACEHOLDER_REGEX[part])
        else:
            out.append(re.escape(part))
    return "".join(out)


def render_severity_donut(breakdown_rows):
    """Pure-SVG donut chart of the severity breakdown, built with the
    classic stroke-dasharray-on-a-circle technique (no charting library,
    no arc-path trigonometry to get wrong). Rounded end-caps plus a
    small gap between segments give it a more polished, modern look
    while keeping the exact same "just draw arcs on a circle"
    implementation. Returns "" when there's nothing to chart so callers
    can skip the wrapping markup entirely.
    """
    rows = [(priority, count) for priority, count in breakdown_rows if count]
    total = sum(count for _, count in rows)
    if not rows or total <= 0:
        return ""

    radius = 74
    stroke_width = 30
    cx = cy = 100
    circumference = 2 * math.pi * radius
    # Visual gap between adjacent segments, as an arc length in the same
    # units as the dasharray below - shaves a touch off each segment's
    # own length so the rounded end-caps don't visually merge into the
    # next segment. Skipped when there's only one segment (100% share -
    # nothing to separate it from).
    gap = 3.0 if len(rows) > 1 else 0.0

    segments = []
    cumulative = 0.0
    for priority, count in rows:
        fraction = count / total
        raw_length = fraction * circumference
        dash_length = max(raw_length - gap, 0.1)
        color = SEVERITY_COLORS.get(priority, "#999999")
        name = SEVERITY_NAMES.get(priority, "Unknown ({0})".format(priority))
        segments.append(
            '<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="{sw}" '
            'stroke-linecap="round" '
            'stroke-dasharray="{dash:.2f} {rest:.2f}" stroke-dashoffset="{offset:.2f}" '
            'transform="rotate(-90 {cx} {cy})"><title>{title}</title></circle>'.format(
                cx=cx, cy=cy, r=radius, color=color, sw=stroke_width,
                dash=dash_length, rest=circumference - dash_length, offset=-cumulative,
                title=escape("{0}: {1} ({2:.0f}%)".format(name, count, fraction * 100)),
            )
        )
        cumulative += raw_length

    svg = (
        '<svg viewBox="0 0 200 200" width="200" height="200" role="img" '
        'aria-label="Severity breakdown donut chart">'
        + "".join(segments)
        + '<text x="100" y="94" text-anchor="middle" font-size="26" font-weight="700" '
          'font-variant-numeric="tabular-nums" fill="#1a1d21">{0}</text>'.format(total)
        + '<text x="100" y="115" text-anchor="middle" font-size="11" fill="#5b6067">log line(s)</text>'
        + "</svg>"
    )
    legend = "".join(
        '<div class="legend-item"><span class="legend-swatch" style="background:{color}"></span>'
        "{name}<span class=\"legend-count\">{count} ({pct:.0f}%)</span></div>".format(
            color=SEVERITY_COLORS.get(priority, "#999999"),
            name=escape(SEVERITY_NAMES.get(priority, "Unknown ({0})".format(priority))),
            count=escape(str(count)),
            pct=(count / total) * 100.0,
        )
        for priority, count in rows
    )
    return '  <div class="donut-wrap">' + svg + '<div class="legend">' + legend + "</div></div>"


def render_volume_trend_chart(trend_counts, since, window):
    """Pure-SVG stacked bar chart of row counts per time bucket, split
    and coloured by severity (see WINDOW_BUCKET_SECONDS / _floor_to_bucket
    for how buckets are chosen and aligned with the SQL side, and
    SEVERITY_COLORS for the shared colour ramp also used by the donut
    chart). Each bar is a stack of one segment per severity level
    present in that bucket, ordered so the most severe colours sit on
    top - a spike's *composition* is visible at a glance instead of
    just its height, and hovering any segment shows that severity's
    exact count plus the bucket's total. Fills in zero-count gaps so
    the chart is a continuous timeline rather than only showing buckets
    that had data. Returns a "no data" message, never raises, when
    there is nothing to plot.
    """
    bucket_seconds = WINDOW_BUCKET_SECONDS.get(window, 3600)
    label_fmt = WINDOW_BUCKET_LABEL_FMT.get(window, "%H:%M")
    step = timedelta(seconds=bucket_seconds)

    start = _floor_to_bucket(since, bucket_seconds)
    end = _floor_to_bucket(datetime.now(), bucket_seconds)
    buckets = []
    cur = start
    while cur <= end:
        buckets.append(cur)
        cur += step

    if not buckets:
        return '  <p class="meta">No log data in this window.</p>'

    # Severities that actually occur anywhere in this window, ascending
    # (0=Emergency first) - drives both stacking order and the legend.
    present_priorities = sorted(set(priority for (_, priority) in trend_counts))
    if not present_priorities:
        return '  <p class="meta">No log data in this window.</p>'

    per_bucket = [
        {priority: trend_counts.get((b, priority), 0) for priority in present_priorities}
        for b in buckets
    ]
    totals = [sum(row.values()) for row in per_bucket]
    max_count = max(totals)
    if max_count <= 0:
        return '  <p class="meta">No log data in this window.</p>'

    n = len(buckets)
    chart_height = 130
    bar_gap = 3.0
    bar_width = max(3.0, min(28.0, (720.0 - bar_gap * (n - 1)) / n)) if n > 1 else 40.0
    total_width = (n - 1) * (bar_width + bar_gap) + bar_width if n > 0 else bar_width

    bars = []
    for i, row in enumerate(per_bucket):
        total = totals[i]
        if total <= 0:
            continue
        x = i * (bar_width + bar_gap)
        label = buckets[i].strftime(label_fmt)
        y_cursor = chart_height
        # Draw least-severe (highest priority number, e.g. 7=Debug)
        # first so it lands at the bottom of the stack, leaving the
        # most-severe (lowest priority number) segment on top where
        # it's the first thing the eye catches.
        for priority in sorted(present_priorities, reverse=True):
            count = row[priority]
            if count <= 0:
                continue
            seg_height = (count / max_count) * (chart_height - 20)
            seg_height = max(seg_height, 1.2)
            y = y_cursor - seg_height
            color = SEVERITY_COLORS.get(priority, "#999999")
            name = SEVERITY_NAMES.get(priority, "Unknown ({0})".format(priority))
            bars.append(
                '<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="{color}">'
                "<title>{title}</title></rect>".format(
                    x=x, y=y, w=bar_width, h=seg_height, color=color,
                    title=escape("{0}: {1} total - {2}: {3}".format(label, total, name, count)),
                )
            )
            y_cursor = y

    label_indices = sorted(set([0, n // 2, n - 1]))
    axis_labels = []
    for i in label_indices:
        x = i * (bar_width + bar_gap) + bar_width / 2
        axis_labels.append(
            '<text x="{x:.1f}" y="{y}" text-anchor="middle" font-size="10" '
            'font-variant-numeric="tabular-nums" fill="#5b6067">{label}</text>'.format(
                x=x, y=chart_height + 16, label=escape(buckets[i].strftime(label_fmt))
            )
        )

    # A single baseline under the bars grounds the chart (real data-ink,
    # not decoration) without the visual noise of full gridlines.
    baseline = (
        '<line x1="0" y1="{y}" x2="{w:.1f}" y2="{y}" stroke="#e2e5ea" stroke-width="1"/>'.format(
            y=chart_height, w=max(total_width, 1.0)
        )
    )

    svg = (
        '<svg viewBox="0 0 {w:.1f} {h}" width="100%" height="150" preserveAspectRatio="none" '
        'role="img" aria-label="Alert volume over time, stacked bar chart coloured by severity">'.format(
            w=max(total_width, 1.0), h=chart_height + 24
        )
        + baseline
        + "".join(bars)
        + "".join(axis_labels)
        + "</svg>"
    )
    legend = "".join(
        '<div class="legend-item"><span class="legend-swatch" style="background:{color}"></span>'
        "{name}</div>".format(
            color=SEVERITY_COLORS.get(priority, "#999999"),
            name=escape(SEVERITY_NAMES.get(priority, "Unknown ({0})".format(priority))),
        )
        for priority in present_priorities
    )
    return '  <div class="trend-chart">' + svg + '<div class="trend-legend">' + legend + "</div></div>"


def _format_kpi_delta(current, previous):
    """Renders the small 'vs previous window' delta line shown under each
    KPI card. `previous` is the same metric computed over the
    immediately-preceding period of equal length (see render_insights_page()
    - prev_kpis). Special-cases a zero previous value (percentage change is
    undefined/infinite in that case) by labelling it 'New' when the current
    count is nonzero, or omitting the delta entirely when both are zero (no
    activity in either period - nothing meaningful to compare)."""
    if previous is None:
        return ""
    if previous == 0:
        if current == 0:
            return ""
        return '<div class="delta delta-up">New (0 in previous window)</div>'
    pct = ((current - previous) / float(previous)) * 100.0
    if current == previous:
        return '<div class="delta delta-flat">No change vs previous window ({0})</div>'.format(escape(str(previous)))
    arrow = "\u25b2" if pct > 0 else "\u25bc"
    cls = "delta-up" if pct > 0 else "delta-down"
    return '<div class="delta {cls}">{arrow} {pct:+.0f}% vs previous {prev}</div>'.format(
        cls=cls, arrow=arrow, pct=pct, prev=escape(str(previous))
    )


def render_kpi_cards(kpis, since, window, host=None, prev_kpis=None):
    ssh_link = build_drilldown_link(q=SSH_FAILURE_PATTERN, mode="regex", host=host or "", tag="sshd", since=since, window=window)
    # "Sudo commands run" isn't a row-level filter by itself either - the
    # Top sudo activity table further down this same page is the per-
    # user/per-command breakdown behind this count (who became sudo and
    # what they ran), so jump straight to it instead of an undifferentiated
    # flat search result.
    sudo_link = "#top-sudo-activity"
    password_link = build_drilldown_link(q=PASSWORD_FAILURE_PATTERN, mode="regex", host=host or "", since=since, window=window)
    # "Distinct source IPs" isn't a row-level filter by itself - the Top
    # source IPs table further down this same page is already the
    # per-IP breakdown behind this count, so jump straight to it.
    ips_link = "#top-source-ips"

    cards = [
        ("SSH auth failures", kpis["ssh_auth_failures"], ssh_link, "ssh_auth_failures"),
        ("Sudo commands run", kpis["sudo_commands"], sudo_link, "sudo_commands"),
        ("Password failures (all sources)", kpis["password_failures"], password_link, "password_failures"),
        ("Distinct source IPs (failed SSH)", kpis["distinct_failed_ssh_ips"], ips_link, "distinct_failed_ssh_ips"),
    ]
    cards_html = "".join(
        '<a class="kpi-card" href="{href}"><div class="value">{value}</div><div class="label">{label}</div>{delta}</a>'.format(
            href=escape(href), value=escape(str(value)), label=escape(label),
            delta=_format_kpi_delta(value, prev_kpis[key] if prev_kpis else None),
        )
        for label, value, href, key in cards
    )
    return '  <div class="kpi-grid">' + cards_html + "</div>"


def render_severity_table(breakdown_rows, since, window, host=None):
    if not breakdown_rows:
        return '  <p class="meta">No log data in this window.</p>'
    lines = ["  <table>", "    <tr><th>Severity</th><th>Count</th><th>Log lines</th></tr>"]
    for priority, count in breakdown_rows:
        name = SEVERITY_NAMES.get(priority, "Unknown ({0})".format(priority))
        drill_link = build_drilldown_link(priority=priority, host=host or "", since=since, window=window)
        lines.append(
            "    <tr><td>{name}</td><td>{count}</td>"
            '<td class="drill"><a href="{drill}">View log lines</a></td></tr>'.format(
                name=escape(name), count=escape(str(count)), drill=escape(drill_link)
            )
        )
    lines.append("  </table>")
    return "\n".join(lines)


def render_top_patterns_table(rows, cfg, since, window, host=None, suppressed=None):
    """suppressed, if given, is the set of (service, pattern) tuples
    currently dismissed via the suppression table (see
    fetch_suppressed_patterns()) - rows matching one of those pairs are
    left out of the table entirely and instead only show up on the
    'Suppressed patterns' sub-view, so a benign pattern you've already
    reviewed stops resurfacing here every time you check. A row with an
    empty-string pattern means the whole service was dismissed via
    'Dismiss all' (see suppress_form below) rather than one exact
    pattern - so it also hides every other (still-varying) pattern from
    that same service, e.g. a repeating traceback whose file/line text
    differs message to message. Every remaining row gets two actions
    that POST to /insights/suppress: 'Dismiss' (this exact pattern
    only) and 'Dismiss all' (this entire service, regardless of
    pattern)."""
    suppressed = suppressed or set()
    services_suppressed_wholesale = {svc for svc, pat in suppressed if pat == ""}
    visible_rows = [
        row for row in rows
        if (row[1] or "").strip().rstrip(":") not in services_suppressed_wholesale
        and ((row[1] or "").strip().rstrip(":"), row[0] or "") not in suppressed
    ]
    suppressed_count = len(rows) - len(visible_rows)
    lines = []
    if suppressed_count:
        lines.append(
            '  <p class="suppressed-note">{0} pattern(s) hidden because they are suppressed - '
            'see <a href="#suppressed-patterns">Suppressed patterns</a> below.</p>'.format(suppressed_count)
        )
    if not visible_rows:
        lines.append('  <p class="meta">No repeated patterns found in this window.</p>')
        return "\n".join(lines)
    lines.append('  <div class="table-scroll">')
    lines.append("  <table>")
    lines.append(
        "    <tr><th>Count</th><th>Service</th><th>Severity</th><th>Hosts</th>"
        "<th>Src IPs</th><th>Pattern</th><th>Assessment</th><th>Log lines</th><th>Actions</th></tr>"
    )
    for row in visible_rows:
        pattern, service, min_priority, cnt, distinct_hosts, distinct_source_ips, first_seen, last_seen, sample_message = row
        verdict, rationale = assess_pattern(row, cfg)
        severity_name = SEVERITY_NAMES.get(min_priority, "Unknown")
        sample = (sample_message or "")[:200]
        service_clean = (service or "").strip().rstrip(":")
        # 'service' has already had its '[<pid>]' stripped (see
        # TOP_PATTERNS_QUERY above) and keeps its trailing ':', but the raw
        # syslogtag column being searched still has the PID in between
        # (e.g. 'sshd[9001]:') - strip the ':' so the substring still matches.
        drill_link = build_drilldown_link(
            q=pattern_to_regex(pattern), mode="regex", host=host or "", tag=service_clean, since=since,
            window=window,
        )
        suppress_form = (
            '<form method="post" action="/insights/suppress" class="suppress-form">'
            '<input type="hidden" name="service" value="{service}">'
            '<input type="hidden" name="pattern" value="{pattern}">'
            '<input type="hidden" name="window" value="{window}">'
            '<input type="hidden" name="host" value="{host}">'
            '<input type="text" name="note" placeholder="note (optional)">'
            '<button type="submit" name="scope" value="pattern" class="suppress-btn">Dismiss</button>'
            '<button type="submit" name="scope" value="service" class="suppress-btn-all" '
            'title="Hide every pattern from this service, not just this exact wording">Dismiss all</button>'
            "</form>"
        ).format(
            service=escape(service_clean), pattern=escape(pattern or ""),
            window=escape(window or ""), host=escape(host or ""),
        )
        lines.append(
            "    <tr><td>{cnt}</td><td>{service}</td><td>{sev}</td><td>{hosts}</td><td>{ips}</td>"
            '<td class="msg">{pattern}<div class="rationale">e.g. {sample}</div></td>'
            "<td>{badge}<div class=\"rationale\">{rationale}</div></td>"
            '<td class="drill"><a href="{drill}">View log lines</a></td>'
            "<td>{suppress_form}</td></tr>".format(
                cnt=escape(str(cnt)),
                service=escape(service or ""),
                sev=escape(severity_name),
                hosts=escape(str(distinct_hosts)),
                ips=escape(str(distinct_source_ips or 0)),
                pattern=escape(pattern or ""),
                sample=escape(sample),
                badge=render_badge(verdict),
                rationale=escape(rationale),
                drill=escape(drill_link),
                suppress_form=suppress_form,
            )
        )
    lines.append("  </table>")
    lines.append("  </div>")
    return "\n".join(lines)


def render_suppressed_patterns_section(suppressed_rows, window, host=None, current_pattern_rows=None):
    """Management sub-view listing every currently-suppressed (service,
    pattern) fingerprint, each with an 'Un-suppress' action that POSTs
    to /insights/unsuppress. suppressed_rows is the raw output of
    fetch_suppressed_patterns_full().

    current_pattern_rows, if given, is the same raw row list passed to
    render_top_patterns_table() for the CURRENT window/host - used only
    to let a 'Dismiss all' (whole-service) row show what it is actually
    hiding right now, since the wildcard itself carries no single
    pattern of its own."""
    rows_by_service = {}
    for row in (current_pattern_rows or []):
        pattern, service, cnt = row[0], row[1], row[3]
        service_clean = (service or "").strip().rstrip(":")
        rows_by_service.setdefault(service_clean, []).append((pattern or "", cnt))
    for service_clean in rows_by_service:
        rows_by_service[service_clean].sort(key=lambda p: p[1], reverse=True)

    lines = ['  <div class="suppressed-section" id="suppressed-patterns">']
    if not suppressed_rows:
        lines.append("  <h2>Suppressed patterns</h2>")
        lines.append('  <p class="meta">No patterns are currently suppressed.</p>')
        lines.append("  </div>")
        return "\n".join(lines)
    # Collapsed by default so this section stops making the page longer as
    # more patterns get dismissed over time - see the .suppressed-details /
    # .suppressed-scroll CSS comment above for the full rationale.
    lines.append('  <details class="suppressed-details">')
    lines.append(
        "    <summary>Suppressed patterns ({0})</summary>".format(len(suppressed_rows))
    )
    lines.append(
        '    <p class="meta">These patterns are hidden from "Top repeated / similar alerts" above '
        "because they have already been reviewed and judged benign.</p>"
    )
    lines.append('    <div class="table-scroll suppressed-scroll">')
    lines.append("    <table>")
    lines.append("      <tr><th>Service</th><th>Pattern</th><th>Note</th><th>Suppressed by</th><th>Suppressed at</th><th>Actions</th></tr>")
    for service, pattern, note, suppressed_by, suppressed_at in suppressed_rows:
        unsuppress_form = (
            '<form method="post" action="/insights/unsuppress" class="suppress-form">'
            '<input type="hidden" name="service" value="{service}">'
            '<input type="hidden" name="pattern" value="{pattern}">'
            '<input type="hidden" name="window" value="{window}">'
            '<input type="hidden" name="host" value="{host}">'
            '<button type="submit" class="unsuppress-btn">Un-suppress</button>'
            "</form>"
        ).format(
            service=escape(service or ""), pattern=escape(pattern or ""),
            window=escape(window or ""), host=escape(host or ""),
        )
        # An empty pattern means this row came from the "Dismiss all" button
        # (see render_top_patterns_table) rather than one exact wording -
        # label it clearly so it's obvious why a whole service disappeared,
        # and let the reviewer expand a live list of exactly which
        # patterns from that service are being hidden right now, so
        # "Dismiss all" is never a black box.
        pattern_cell = escape(pattern) if pattern else '(all patterns from this service)'
        if not pattern:
            hidden = rows_by_service.get((service or "").strip().rstrip(":"), [])
            if hidden:
                shown, extra = hidden[:25], hidden[25:]
                items = "".join(
                    "<li>{0} <span class=\"hidden-count\">(count {1})</span></li>".format(
                        escape(p), escape(str(c))
                    )
                    for p, c in shown
                )
                if extra:
                    items += "<li><em>+{0} more not shown</em></li>".format(len(extra))
                pattern_cell += (
                    '<details class="hidden-patterns">'
                    "<summary>Show {0} pattern(s) currently hidden in this window</summary>"
                    "<ul>{1}</ul>"
                    "</details>"
                ).format(len(hidden), items)
            else:
                pattern_cell += (
                    '<p class="meta hidden-patterns-empty">'
                    "No patterns from this service appear in the current window.</p>"
                )
        lines.append(
            "      <tr><td>{service}</td><td class=\"msg\">{pattern}</td><td>{note}</td>"
            "<td>{by}</td><td>{at}</td><td>{actions}</td></tr>".format(
                service=escape(service or ""),
                pattern=pattern_cell,
                note=escape(note or ""),
                by=escape(suppressed_by or ""),
                at=escape(str(suppressed_at) if suppressed_at else ""),
                actions=unsuppress_form,
            )
        )
    lines.append("    </table>")
    lines.append("    </div>")
    lines.append("  </details>")
    lines.append("  </div>")
    return "\n".join(lines)


def render_top_ips_table(rows, cfg, since, window, host=None):
    if not rows:
        return '  <p class="meta">No failed SSH logins with a detectable source IP in this window.</p>'
    lines = [
        "  <table>",
        "    <tr><th>Source IP</th><th>Attempts</th><th>Distinct usernames</th>"
        "<th>Distinct target hosts</th><th>Assessment</th><th>Log lines</th></tr>",
    ]
    for row in rows:
        source_ip, attempts, distinct_users, distinct_targets, first_seen, last_seen = row
        verdict, rationale = assess_ip(row, cfg)
        if source_ip:
            drill_link = build_drilldown_link(q=source_ip, mode="contains", host=host or "", tag="sshd", since=since, window=window)
            drill_cell = '<a href="{0}">View log lines</a>'.format(escape(drill_link))
        else:
            drill_cell = ""
        lines.append(
            "    <tr><td>{ip}</td><td>{attempts}</td><td>{users}</td><td>{targets}</td>"
            "<td>{badge}<div class=\"rationale\">{rationale}</div></td>"
            '<td class="drill">{drill}</td></tr>'.format(
                ip=escape(source_ip or "(unknown)"),
                attempts=escape(str(attempts)),
                users=escape(str(distinct_users or 0)),
                targets=escape(str(distinct_targets or 0)),
                badge=render_badge(verdict),
                rationale=escape(rationale),
                drill=drill_cell,
            )
        )
    lines.append("  </table>")
    return "\n".join(lines)


def render_top_sudo_table(rows, cfg, since, window, host=None):
    if not rows:
        return '  <p class="meta">No sudo commands recorded in this window.</p>'
    lines = [
        "  <table>",
        "    <tr><th>Invoking user</th><th>Became</th><th>Command</th><th>Count</th>"
        "<th>Hosts</th><th>Assessment</th><th>Log lines</th></tr>",
    ]
    for row in rows:
        (invoking_user, target_user, base_command, cnt, distinct_hosts,
         first_seen, last_seen, sample_command) = row
        verdict, rationale = assess_sudo(row, cfg)
        sample = (sample_command or "")[:200]
        drill_pattern = (
            re.escape(invoking_user or "") + r"\s*:.*USER=" + re.escape(target_user or "")
            + r".*COMMAND=" + re.escape(base_command or "")
        )
        drill_link = build_drilldown_link(q=drill_pattern, mode="regex", host=host or "", tag="sudo", since=since, window=window)
        lines.append(
            "    <tr><td>{invoking}</td><td>{became}</td>"
            '<td class="msg">{command}<div class="rationale">e.g. {sample}</div></td>'
            "<td>{cnt}</td><td>{hosts}</td>"
            "<td>{badge}<div class=\"rationale\">{rationale}</div></td>"
            '<td class="drill"><a href="{drill}">View log lines</a></td></tr>'.format(
                invoking=escape(invoking_user or "(unknown)"),
                became=escape(target_user or "(unknown)"),
                command=escape(base_command or ""),
                sample=escape(sample),
                cnt=escape(str(cnt)),
                hosts=escape(str(distinct_hosts or 0)),
                badge=render_badge(verdict),
                rationale=escape(rationale),
                drill=escape(drill_link),
            )
        )
    lines.append("  </table>")
    return "\n".join(lines)


def render_top_su_table(rows, cfg, since, window, host=None):
    if not rows:
        return '  <p class="meta">No su sessions recorded in this window.</p>'
    lines = [
        "  <table>",
        "    <tr><th>Invoking user</th><th>Became</th><th>Count</th>"
        "<th>Hosts</th><th>Assessment</th><th>Log lines</th></tr>",
    ]
    for row in rows:
        invoking_user, target_user, cnt, distinct_hosts, first_seen, last_seen = row
        verdict, rationale = assess_su(row, cfg)
        # su has no COMMAND= field to anchor on (see SU_*_CAPTURE_PATTERN
        # above), so the drill-down regex ties to the invoking/target
        # users in the "for user X ... by Y" session-open wording itself.
        drill_pattern = (
            SU_MESSAGE_ANCHOR_PATTERN + r".*for user " + re.escape(target_user or "")
            + r".*by " + re.escape(invoking_user or "")
        )
        drill_link = build_drilldown_link(q=drill_pattern, mode="regex", host=host or "", tag="su", since=since, window=window)
        lines.append(
            "    <tr><td>{invoking}</td><td>{became}</td>"
            "<td>{cnt}</td><td>{hosts}</td>"
            "<td>{badge}<div class=\"rationale\">{rationale}</div></td>"
            '<td class="drill"><a href="{drill}">View log lines</a></td></tr>'.format(
                invoking=escape(invoking_user or "(unknown)"),
                became=escape(target_user or "(unknown)"),
                cnt=escape(str(cnt)),
                hosts=escape(str(distinct_hosts or 0)),
                badge=render_badge(verdict),
                rationale=escape(rationale),
                drill=escape(drill_link),
            )
        )
    lines.append("  </table>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CSV report exports (downloadable from the Insights page) - management
# summary and full ops-detail. Both are built as plain lists of
# variable-width rows (blank rows / header rows separate sections, a
# common convention for flat multi-table CSVs) and written with the
# stdlib csv module - no template/HTML involved, so html.escape() is not
# needed here; every value is either a DB-derived number/string or one
# of our own fixed labels.
# ---------------------------------------------------------------------------

REPORT_KINDS = ("management", "ops")


def build_management_summary_rows(kpis, severity_rows, since, window, host=None):
    total = sum(count for _, count in severity_rows)
    high_severity = sum(count for priority, count in severity_rows if priority is not None and priority <= 3)
    rows = [
        ["Log search / insights - management summary"],
        ["Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["Window", WINDOW_LABELS.get(window, window)],
        ["Host filter", host or "(fleet-wide)"],
        [],
        ["Section", "Metric", "Value"],
        ["KPI", "SSH auth failures", kpis["ssh_auth_failures"]],
        ["KPI", "Sudo commands run", kpis["sudo_commands"]],
        ["KPI", "Password failures (all sources)", kpis["password_failures"]],
        ["KPI", "Distinct source IPs (failed SSH)", kpis["distinct_failed_ssh_ips"]],
    ]
    for priority, count in severity_rows:
        rows.append(["Severity breakdown", SEVERITY_NAMES.get(priority, "Unknown ({0})".format(priority)), count])
    rows.append([])
    rows.append(["Overall assessment", "Total log lines in window", total])
    rows.append(["Overall assessment", "High-severity (Emergency-Error) lines", high_severity])
    rows.append(
        [
            "Overall assessment",
            "Note",
            "Heuristic counts only - not a substitute for manual review or a real SIEM.",
        ]
    )
    return rows


def build_ops_detail_rows(kpis, severity_rows, pattern_rows, ip_rows, sudo_rows, su_rows, cfg, since, window, host=None):
    rows = build_management_summary_rows(kpis, severity_rows, since, window, host)

    rows.append([])
    rows.append(["Top repeated / similar alerts"])
    rows.append(
        [
            "Count", "Service", "Severity", "Distinct hosts", "Distinct src IPs",
            "Pattern", "Sample message", "Assessment", "Rationale", "Drill-down URL (relative)",
        ]
    )
    for row in pattern_rows:
        (pattern, service, min_priority, cnt, distinct_hosts, distinct_source_ips,
         first_seen, last_seen, sample_message) = row
        verdict, rationale = assess_pattern(row, cfg)
        drill_link = build_drilldown_link(
            q=pattern_to_regex(pattern), mode="regex", host=host or "", tag=(service or "").strip().rstrip(":"),
            since=since, window=window,
        )
        rows.append(
            [
                cnt, service or "", SEVERITY_NAMES.get(min_priority, "Unknown"), distinct_hosts,
                distinct_source_ips or 0, pattern or "", (sample_message or "")[:200],
                verdict, rationale, drill_link,
            ]
        )

    rows.append([])
    rows.append(["Top source IPs (failed SSH logins)"])
    rows.append(
        ["Source IP", "Attempts", "Distinct usernames", "Distinct target hosts", "Assessment", "Rationale", "Drill-down URL (relative)"]
    )
    for row in ip_rows:
        source_ip, attempts, distinct_users, distinct_targets, first_seen, last_seen = row
        verdict, rationale = assess_ip(row, cfg)
        drill_link = (
            build_drilldown_link(q=source_ip, mode="contains", host=host or "", tag="sshd", since=since, window=window)
            if source_ip else ""
        )
        rows.append([source_ip or "(unknown)", attempts, distinct_users or 0, distinct_targets or 0, verdict, rationale, drill_link])

    rows.append([])
    rows.append(["Top sudo activity"])
    rows.append(
        ["Invoking user", "Became", "Command", "Count", "Distinct hosts", "Sample command", "Assessment", "Rationale", "Drill-down URL (relative)"]
    )
    for row in sudo_rows:
        (invoking_user, target_user, base_command, cnt, distinct_hosts,
         first_seen, last_seen, sample_command) = row
        verdict, rationale = assess_sudo(row, cfg)
        drill_pattern = (
            re.escape(invoking_user or "") + r"\s*:.*USER=" + re.escape(target_user or "")
            + r".*COMMAND=" + re.escape(base_command or "")
        )
        drill_link = build_drilldown_link(q=drill_pattern, mode="regex", host=host or "", tag="sudo", since=since, window=window)
        rows.append(
            [
                invoking_user or "(unknown)", target_user or "(unknown)", base_command or "",
                cnt, distinct_hosts or 0, (sample_command or "")[:200], verdict, rationale, drill_link,
            ]
        )

    rows.append([])
    rows.append(["Top su activity"])
    rows.append(
        ["Invoking user", "Became", "Count", "Distinct hosts", "Assessment", "Rationale", "Drill-down URL (relative)"]
    )
    for row in su_rows:
        invoking_user, target_user, cnt, distinct_hosts, first_seen, last_seen = row
        verdict, rationale = assess_su(row, cfg)
        drill_pattern = (
            SU_MESSAGE_ANCHOR_PATTERN + r".*for user " + re.escape(target_user or "")
            + r".*by " + re.escape(invoking_user or "")
        )
        drill_link = build_drilldown_link(q=drill_pattern, mode="regex", host=host or "", tag="su", since=since, window=window)
        rows.append(
            [
                invoking_user or "(unknown)", target_user or "(unknown)",
                cnt, distinct_hosts or 0, verdict, rationale, drill_link,
            ]
        )

    return rows


def render_insights_export_csv(qs):
    """Builds one of the two downloadable Insights reports. Returns
    (csv_bytes, filename, error_message) - exactly one of csv_bytes/error
    is set. The 'report' query param is validated against the fixed
    REPORT_KINDS whitelist and silently falls back to 'management' on
    anything else, matching the tolerant-validation style already used
    by parse_window()/parse_priority() elsewhere in this file.
    """
    window = parse_window(_first(qs, "window", config.INSIGHTS_DEFAULT_WINDOW), config.INSIGHTS_DEFAULT_WINDOW)
    if window not in WINDOW_CHOICES:
        window = "24h"
    since = datetime.now() - WINDOW_CHOICES[window]
    host = (_first(qs, "host", "") or "").strip()

    report_kind = _first(qs, "report", "management")
    if report_kind not in REPORT_KINDS:
        report_kind = "management"

    cfg = {
        "repeat_ip_threshold": config.INSIGHTS_REPEAT_IP_THRESHOLD,
        "bruteforce_count_threshold": config.INSIGHTS_BRUTEFORCE_ATTEMPTS_THRESHOLD,
        "bruteforce_users_threshold": config.INSIGHTS_BRUTEFORCE_USERS_THRESHOLD,
        "high_severity_repeat_threshold": config.INSIGHTS_HIGH_SEVERITY_REPEAT_THRESHOLD,
        "low_count_threshold": config.INSIGHTS_LOW_COUNT_THRESHOLD,
        "sudo_repeat_threshold": config.INSIGHTS_SUDO_REPEAT_THRESHOLD,
        "sudo_sensitive_re": re.compile(config.INSIGHTS_SUDO_SENSITIVE_COMMANDS_PATTERN, re.I),
        "su_repeat_threshold": config.INSIGHTS_SU_REPEAT_THRESHOLD,
    }

    kpis = severity_rows = pattern_rows = ip_rows = sudo_rows = su_rows = None
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                kpis = fetch_kpis(cur, config.DB_TABLE, since, host=host)
                severity_rows = fetch_severity_breakdown(cur, config.DB_TABLE, since, host=host)
                if report_kind == "ops":
                    pattern_rows = fetch_top_patterns(
                        cur, config.DB_TABLE, since, config.INSIGHTS_SCAN_ROWS, config.INSIGHTS_TOP_ALERTS, host=host
                    )
                    ip_rows = fetch_top_source_ips(cur, config.DB_TABLE, since, config.INSIGHTS_TOP_IPS, host=host)
                    sudo_rows = fetch_top_sudo_activity(
                        cur, config.DB_TABLE, since, config.INSIGHTS_SCAN_ROWS, config.INSIGHTS_TOP_SUDO_USERS, host=host
                    )
                    su_rows = fetch_top_su_activity(
                        cur, config.DB_TABLE, since, config.INSIGHTS_SCAN_ROWS, config.INSIGHTS_TOP_SU_USERS, host=host
                    )
    except Exception:  # noqa: BLE001 - surface a friendly message, log detail server-side
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return None, None, "Report export failed - check the application's error output for details ('journalctl -u log-search-ui')."

    if report_kind == "ops":
        rows = build_ops_detail_rows(kpis, severity_rows, pattern_rows, ip_rows, sudo_rows, su_rows, cfg, since, window, host)
        name_part = "ops-detail"
    else:
        rows = build_management_summary_rows(kpis, severity_rows, since, window, host)
        name_part = "management-summary"

    # window is already validated against the fixed WINDOW_CHOICES
    # whitelist above, so it's safe to embed directly in a filename /
    # Content-Disposition header with no quoting concerns.
    filename = "log-insights-{0}-{1}-{2}.csv".format(name_part, window, datetime.now().strftime("%Y%m%d-%H%M%S"))

    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8"), filename, None


def render_insights_page(qs):
    window = parse_window(_first(qs, "window", config.INSIGHTS_DEFAULT_WINDOW), config.INSIGHTS_DEFAULT_WINDOW)
    if window not in WINDOW_CHOICES:
        window = "24h"
    window_delta = WINDOW_CHOICES[window]
    since = datetime.now() - window_delta
    # Trend-delta comparison period: the immediately-preceding window of
    # the exact same length, e.g. for a 24h window that's the 24h block
    # right before the current one - [since_prev, since). Same query
    # shape fetch_kpis() already runs, just called a second time with an
    # upper bound added (see _base_where()).
    since_prev = since - window_delta
    host = (_first(qs, "host", "") or "").strip()

    subtitle = '  <p class="subtitle">Heuristic insights over {0}{1}.</p>'.format(
        escape(WINDOW_LABELS.get(window, window)),
        " for host matching \u201c{0}\u201d".format(escape(host)) if host else "",
    )
    body = [page_head("insights"), subtitle, render_window_form(window, host)]

    cfg = {
        "repeat_ip_threshold": config.INSIGHTS_REPEAT_IP_THRESHOLD,
        "bruteforce_count_threshold": config.INSIGHTS_BRUTEFORCE_ATTEMPTS_THRESHOLD,
        "bruteforce_users_threshold": config.INSIGHTS_BRUTEFORCE_USERS_THRESHOLD,
        "high_severity_repeat_threshold": config.INSIGHTS_HIGH_SEVERITY_REPEAT_THRESHOLD,
        "low_count_threshold": config.INSIGHTS_LOW_COUNT_THRESHOLD,
        "sudo_repeat_threshold": config.INSIGHTS_SUDO_REPEAT_THRESHOLD,
        "sudo_sensitive_re": re.compile(config.INSIGHTS_SUDO_SENSITIVE_COMMANDS_PATTERN, re.I),
        "su_repeat_threshold": config.INSIGHTS_SU_REPEAT_THRESHOLD,
    }

    error = None
    kpis = prev_kpis = severity_rows = pattern_rows = ip_rows = sudo_rows = su_rows = trend_counts = None
    suppressed = set()
    suppressed_full = []
    try:
        # These 10 fetches are all independent reads with no data
        # dependency on each other, so they run on their own connections
        # concurrently (see _fetch_isolated()) instead of queued one
        # after another on a single shared connection - wall-clock time
        # for the page is then bounded by the slowest single query
        # instead of the sum of all of them.
        with concurrent.futures.ThreadPoolExecutor(max_workers=INSIGHTS_QUERY_WORKERS) as executor:
            futures = {
                "kpis": executor.submit(_fetch_isolated, fetch_kpis, config.DB_TABLE, since, host=host),
                "prev_kpis": executor.submit(
                    _fetch_isolated, fetch_kpis, config.DB_TABLE, since_prev, until=since, host=host
                ),
                "severity_rows": executor.submit(
                    _fetch_isolated, fetch_severity_breakdown, config.DB_TABLE, since, host=host
                ),
                "pattern_rows": executor.submit(
                    _fetch_isolated,
                    fetch_top_patterns,
                    config.DB_TABLE,
                    since,
                    config.INSIGHTS_SCAN_ROWS,
                    config.INSIGHTS_TOP_ALERTS,
                    host=host,
                ),
                "ip_rows": executor.submit(
                    _fetch_isolated, fetch_top_source_ips, config.DB_TABLE, since, config.INSIGHTS_TOP_IPS, host=host
                ),
                "sudo_rows": executor.submit(
                    _fetch_isolated,
                    fetch_top_sudo_activity,
                    config.DB_TABLE,
                    since,
                    config.INSIGHTS_SCAN_ROWS,
                    config.INSIGHTS_TOP_SUDO_USERS,
                    host=host,
                ),
                "su_rows": executor.submit(
                    _fetch_isolated,
                    fetch_top_su_activity,
                    config.DB_TABLE,
                    since,
                    config.INSIGHTS_SCAN_ROWS,
                    config.INSIGHTS_TOP_SU_USERS,
                    host=host,
                ),
                "trend_counts": executor.submit(
                    _fetch_isolated,
                    fetch_volume_trend,
                    config.DB_TABLE,
                    since,
                    WINDOW_BUCKET_SECONDS.get(window, 3600),
                    host=host,
                ),
                "suppressed": executor.submit(_fetch_isolated, fetch_suppressed_patterns),
                "suppressed_full": executor.submit(_fetch_isolated, fetch_suppressed_patterns_full),
            }
            # .result() re-raises any exception from that future - the
            # first one encountered propagates out to the except block
            # below exactly as a sequential call would have, so a
            # single failing query still surfaces the same friendly
            # error as before, not a partially-rendered page.
            results = {key: future.result() for key, future in futures.items()}

        kpis = results["kpis"]
        prev_kpis = results["prev_kpis"]
        severity_rows = results["severity_rows"]
        pattern_rows = results["pattern_rows"]
        ip_rows = results["ip_rows"]
        sudo_rows = results["sudo_rows"]
        su_rows = results["su_rows"]
        trend_counts = results["trend_counts"]
        suppressed = results["suppressed"]
        suppressed_full = results["suppressed_full"]
    except Exception:  # noqa: BLE001 - surface a friendly message, log detail server-side
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        error = "Insights failed to load - check the application's error output for details ('journalctl -u log-search-ui')."

    if error:
        body.append('  <div class="error">{0}</div>'.format(escape(error)))
    else:
        export_qs = {"window": window}
        if host:
            export_qs["host"] = host
        management_link = "/insights/export.csv?" + urlencode(dict(export_qs, report="management"))
        ops_link = "/insights/export.csv?" + urlencode(dict(export_qs, report="ops"))
        # &#8681; prefix + "Download ... as CSV" wording matches the single
        # CSV link on the LinuxDB Overview page (roles/linuxdb_dashboard) -
        # keep both in sync if either wording ever changes.
        body.append(
            '  <p class="report-links">'
            '<a href="{mgmt}">&#8681; Download management summary as CSV</a> &middot; '
            '<a href="{ops}">&#8681; Download full ops-detail report as CSV</a></p>'.format(mgmt=escape(management_link), ops=escape(ops_link))
        )
        # Always-visible, regardless of whether the current window/host
        # filter happens to show any hidden rows right now (the per-table
        # "N pattern(s) hidden" note below only appears when a suppressed
        # pattern actually matches a row in the current view) - so there is
        # always a one-click way to review or un-suppress anything you've
        # dismissed, even from a different window/host than when you
        # dismissed it.
        body.append(
            '  <p class="meta"><a href="#suppressed-patterns">Suppressed / dismissed items ({0})</a></p>'.format(
                len(suppressed_full)
            )
        )
        body.append(render_kpi_cards(kpis, since, window, host=host, prev_kpis=prev_kpis))
        body.append("  <h2>Alert volume over time</h2>")
        body.append(render_volume_trend_chart(trend_counts, since, window))
        body.append("  <h2>Severity breakdown</h2>")
        donut_html = render_severity_donut(severity_rows)
        table_html = render_severity_table(severity_rows, since, window, host=host)
        if donut_html:
            body.append(
                '  <div class="severity-flex">' + donut_html + '<div class="severity-table-col">' + table_html + "</div></div>"
            )
        else:
            body.append(table_html)
        body.append("  <h2>Top repeated / similar alerts</h2>")
        body.append(
            '  <p class="meta">Scanned up to {0} most recent row(s) in this window. '
            'Click "View log lines" on a row to see the actual matching entries. '
            '"Dismiss" hides just this exact pattern; "Dismiss all" hides every pattern from that '
            "service (use it when the same noisy service keeps resurfacing under slightly "
            "different wording, e.g. different line numbers in a repeating traceback).</p>".format(
                escape(str(config.INSIGHTS_SCAN_ROWS))
            )
        )
        body.append(render_top_patterns_table(pattern_rows, cfg, since, window, host=host, suppressed=suppressed))
        body.append('  <h2 id="top-source-ips">Top source IPs (failed SSH logins)</h2>')
        body.append(render_top_ips_table(ip_rows, cfg, since, window, host=host))
        body.append('  <h2 id="top-sudo-activity">Top sudo activity</h2>')
        body.append(
            '  <p class="meta">Who ran sudo, which account they became, and what they ran - '
            'grouped by user, target account, and base command.</p>'
        )
        body.append(render_top_sudo_table(sudo_rows, cfg, since, window, host=host))
        body.append('  <h2 id="top-su-activity">Top su activity</h2>')
        body.append(
            '  <p class="meta">Who ran su, and which account they became. Unlike sudo, su has no '
            "per-command log of its own - it just opens an interactive shell as the target user, so "
            'there is no "Command" column here; grouped by invoking user and target account.</p>'
        )
        body.append(render_top_su_table(su_rows, cfg, since, window, host=host))
        body.append(
            render_suppressed_patterns_section(
                suppressed_full, window, host=host, current_pattern_rows=pattern_rows
            )
        )

    body.append(
        '  <p class="caveat">These counts and the "genuine / review / noise" labels are simple heuristics '
        "based on count, severity, and pattern matching - they are not a substitute for manual review or a "
        "real SIEM. Always verify anything flagged before acting on it.</p>"
    )
    body.append(PAGE_TAIL)
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Authentication - HTTP Basic Auth, checked directly here rather than
# delegated to a reverse proxy - see the "Authentication" section of
# README.md. Stateless: no sessions, no cookies, credentials are re-sent
# (and re-checked) with every request. Deliberately still stdlib-only
# (base64 + hmac), same philosophy as the rest of this app.
# ---------------------------------------------------------------------------


def _unauthorized_response(start_response):
    body = b"401 Unauthorized\n"
    start_response(
        "401 Unauthorized",
        [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("WWW-Authenticate", 'Basic realm="log-search-ui", charset="UTF-8"'),
        ],
    )
    return [body]


_KERBEROS_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _check_kerberos_password(user, password):
    """Verifies user/password against the configured Kerberos realm
    (config.KERBEROS_REALM - FreeIPA/IdM or Active Directory, both just
    Kerberos KDCs) via a throwaway `kinit`, using a dedicated ticket
    cache file that is always destroyed (kdestroy) and removed before
    returning - see the "Kerberos authentication mode" section of
    README.md.

    The password is passed to kinit only via stdin - never as an argv
    element (would be visible in `ps`) and never written to disk. No new
    packages: relies on krb5-workstation's kinit/kdestroy already being
    present on a domain-joined host (see tasks/main.yml, which only
    ever checks for these commands, never installs them).
    """
    realm = getattr(config, "KERBEROS_REALM", "") or ""
    if not realm or not password or not _KERBEROS_USERNAME_RE.match(user or ""):
        return False
    principal = "{0}@{1}".format(user, realm)
    kinit_path = getattr(config, "KERBEROS_KINIT_PATH", "/usr/bin/kinit")
    kdestroy_path = getattr(config, "KERBEROS_KDESTROY_PATH", "/usr/bin/kdestroy")
    try:
        fd, ccache_path = tempfile.mkstemp(prefix="dashboard_krb5cc_")
        os.close(fd)
    except OSError:
        # Fail closed (deny access) rather than let an unexpected
        # filesystem error (SELinux denial, quota, read-only /tmp,
        # etc.) crash out of the WSGI app with a raw 500 page - see
        # "Kerberos authentication mode" troubleshooting in README.md.
        traceback.print_exc(file=sys.stderr)
        return False
    try:
        os.remove(ccache_path)  # kinit creates it fresh; avoid stale/wrong perms
    except OSError:
        pass
    env = dict(os.environ)
    env["KRB5CCNAME"] = "FILE:" + ccache_path
    try:
        result = subprocess.run(
            [kinit_path, principal],
            input=(password + "\n").encode("utf-8"),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        try:
            subprocess.run(
                [kdestroy_path],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            os.remove(ccache_path)
        except OSError:
            pass


def _check_kerberos_group(user):
    """Returns True if config.KERBEROS_REQUIRED_GROUP is unset (no group
    restriction configured), or if `user` is a member of that group per
    either `getent group` (supplementary members) or `id -nG` (also
    covers the user's primary group) - both backed by the same SSSD
    cache already used for name resolution on a domain-joined host.
    """
    required_group = getattr(config, "KERBEROS_REQUIRED_GROUP", "") or ""
    if not required_group:
        return True
    if not _KERBEROS_USERNAME_RE.match(user or ""):
        return False
    getent_path = getattr(config, "KERBEROS_GETENT_PATH", "/usr/bin/getent")
    id_path = getattr(config, "KERBEROS_ID_PATH", "/usr/bin/id")
    try:
        result = subprocess.run(
            [getent_path, "group", required_group],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode == 0:
        line = result.stdout.decode("utf-8", "replace").strip()
        parts = line.split(":")
        members = parts[3].split(",") if len(parts) >= 4 and parts[3] else []
        if user in members:
            return True
    try:
        id_result = subprocess.run(
            [id_path, "-nG", user],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if id_result.returncode != 0:
        return False
    groups = id_result.stdout.decode("utf-8", "replace").split()
    return required_group in groups


def _check_basic_auth(environ):
    """Returns True if the request's Authorization header carries valid
    credentials, per whichever mode config.AUTH_MODE selects:

    - "local" (default): the configured username/password
      (config.AUTH_USER / config.AUTH_PASSWORD), compared with
      hmac.compare_digest() (stdlib, constant-time) rather than == so a
      partially-correct password can't be distinguished from a
      completely wrong one via response-timing side channels.
    - "kerberos": a real Kerberos realm via kinit, optionally also
      requiring group membership - see _check_kerberos_password() and
      _check_kerberos_group() above.
    """
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    if ":" not in decoded:
        return False
    user, _, password = decoded.partition(":")
    if getattr(config, "AUTH_MODE", "local") == "kerberos":
        if not _check_kerberos_password(user, password):
            return False
        return _check_kerberos_group(user)
    user_ok = hmac.compare_digest(user, config.AUTH_USER)
    password_ok = hmac.compare_digest(password, config.AUTH_PASSWORD)
    return user_ok and password_ok


def _extract_auth_username(environ):
    """Best-effort display name for the 'suppressed_by' audit column -
    just decodes the same Basic-auth header _check_basic_auth() already
    validated, so this is never itself a source of truth for identity,
    only a label. Returns '' if it can't be determined (e.g. auth is
    disabled entirely)."""
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return ""
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    user, _, _password = decoded.partition(":")
    return user


def _read_post_form(environ):
    """Reads and parses an application/x-www-form-urlencoded POST body
    (the only content type the suppress/unsuppress forms below ever
    send) using CONTENT_LENGTH from the WSGI environ - stdlib only, no
    cgi module (deprecated/removed upstream). Returns a dict of
    first-value-per-key, matching how _first()/qs already behave
    elsewhere in this file. Caps the read at 64KiB - these forms only
    ever carry a handful of short fields, so anything larger is
    rejected rather than trusted.
    """
    try:
        length = int(environ.get("CONTENT_LENGTH", 0) or 0)
    except ValueError:
        length = 0
    length = max(0, min(length, 65536))
    body = environ["wsgi.input"].read(length) if length else b""
    parsed = parse_qs(body.decode("utf-8", "replace"))
    return {k: v[0] for k, v in parsed.items() if v}


def _redirect_to_insights(start_response, window, host):
    """303 See Other back to /insights with the same window/host the
    suppress/unsuppress form was submitted from - standard
    POST-redirect-GET so reloading the destination page never re-submits
    the form."""
    qs = {"window": window} if window else {}
    if host:
        qs["host"] = host
    # No #suppressed-patterns fragment here on purpose - land back at the
    # top of the page (with the new persistent "Suppressed / dismissed
    # items (N)" link visible) rather than auto-scrolling to the bottom
    # section after every Dismiss/Dismiss all/Un-suppress action.
    location = "/insights" + (("?" + urlencode(qs)) if qs else "")
    start_response("303 See Other", [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", "0"), ("Location", location)])
    return [b""]


# ---------------------------------------------------------------------------
# WSGI entry point - routes on PATH_INFO between the two pages above.
# ---------------------------------------------------------------------------


def application(environ, start_response):
    if getattr(config, "AUTH_ENABLED", True):
        try:
            auth_ok = _check_basic_auth(environ)
        except Exception:  # noqa: BLE001 - fail closed: any unexpected error in
            # the auth check (Kerberos subprocess failure, filesystem error,
            # etc.) denies access instead of crashing the whole WSGI app out
            # to the generic wsgiref "server error" page - see "Kerberos
            # authentication mode" troubleshooting in README.md.
            traceback.print_exc(file=sys.stderr)
            auth_ok = False
        if not auth_ok:
            return _unauthorized_response(start_response)

    path = (environ.get("PATH_INFO", "") or "/").rstrip("/") or "/"
    qs = parse_qs(environ.get("QUERY_STRING", ""))

    if path in ("/insights/suppress", "/insights/unsuppress") and environ.get("REQUEST_METHOD", "GET") == "POST":
        form = _read_post_form(environ)
        service = (form.get("service") or "").strip()
        pattern = (form.get("pattern") or "").strip()
        # scope="service" comes from the "Dismiss all" button (see
        # render_top_patterns_table) - it stores an empty-string pattern,
        # which fetch_suppressed_patterns()/render_top_patterns_table treat
        # as "suppress every pattern from this service", not just the one
        # exact wording that happened to be on screen when it was clicked.
        scope = (form.get("scope") or "pattern").strip()
        effective_pattern = "" if scope == "service" else pattern
        window = parse_window(form.get("window"), config.INSIGHTS_DEFAULT_WINDOW)
        host = (form.get("host") or "").strip()
        if service or pattern:
            try:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        if path == "/insights/suppress":
                            suppress_pattern(
                                cur, service, effective_pattern, form.get("note"), _extract_auth_username(environ)
                            )
                        else:
                            unsuppress_pattern(cur, service, pattern)
                    conn.commit()
            except Exception:  # noqa: BLE001 - log detail server-side, still redirect back to Insights
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
        return _redirect_to_insights(start_response, window, host)

    if path == "/insights/export.csv":
        csv_bytes, filename, error = render_insights_export_csv(qs)
        if error:
            output = error.encode("utf-8")
            start_response(
                "500 Internal Server Error",
                [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(output)))],
            )
            return [output]
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition", 'attachment; filename="{0}"'.format(filename)),
                ("Content-Length", str(len(csv_bytes))),
            ],
        )
        return [csv_bytes]

    if path == "/insights":
        html_out = render_insights_page(qs)
    else:
        html_out = render_search_page(qs)

    output = html_out.encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(output)))])
    return [output]
