#!/usr/bin/env python3
"""
LinuxDB dashboard - read-only web UI over:
  - hostinfo.csv, produced by roles/hostinfo (pipe-delimited linuxdb inventory),
    plus its own timestamped history snapshots (roles/hostinfo saves one
    hostinfo_<timestamp>.csv per run alongside the live file)
  - timestamped diagnostic history, produced by roles/linuxdb_reports
    (disk/filesystem, processes, server health, system stats - one script
    each, run linuxdb-wide and saved per host per run).

Deployed via ansible.builtin.copy (NOT template) because this file
contains literal `{` `}` characters in its embedded CSS block that would
be corrupted if Jinja templated it. Only config.py (rendered from the
brace-free config.py.j2) is templated - see roles/linuxdb_dashboard/tasks.

This app is pure Python standard library - no Flask, no third-party
packages - so it runs on RHEL 8's older python3 unmodified (no walrus
operator, no dict-merge `|` operator, nothing needing 3.8+/3.9+) and
needs nothing beyond what's already required to run Ansible itself.

Security:
  - Read-only. This process never writes to HOSTINFO_CSV_PATH or
    LINUXDB_REPORTS_DATA_DIR - it only ever opens files for reading.
  - Every value taken from the URL (hostname, check key, timestamp,
    hostinfo history snapshot) is validated against a strict allow-list
    pattern, then checked against what actually exists on disk, before
    it's ever used to build a filesystem path. The resolved path is
    also verified to still live under its own data directory (defence
    in depth against path traversal) - see _safe_join().
  - Every dynamic value written into HTML output is passed through
    html.escape().
"""

import base64
import csv
import hmac
import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import parse_qs, quote, unquote

import config

HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{8}_[0-9]{6}$")
CHECK_KEYS = [key for key, _label in config.CHECKS]
CHECK_LABELS = dict(config.CHECKS)
VALID_CHECK_KEYS = set(CHECK_KEYS)

# Config Files tab (roles/config_snapshot data) - a separate feature from
# the linuxdb_reports checks above, but shown as one more tab alongside
# them on the same per-host page rather than a page of its own.
CONFIG_FILES_CHECK_KEY = "config_files"
CONFIG_FILE_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
# getattr(...) fallback guards against the narrow window right after this
# feature first ships, where an existing install's config.py hasn't been
# re-templated yet (e.g. a partial/tag-limited run) - falls back to
# roles/config_snapshot's own default rather than crashing every page.
CONFIG_SNAPSHOT_DATA_DIR = getattr(config, "CONFIG_SNAPSHOT_DATA_DIR", "/linuxdb/configs")

# roles/hostinfo saves one timestamped snapshot per run alongside the
# live hostinfo.csv, in a "history" subdirectory next to it (see that
# role's tasks/report.yml) - derived here rather than as its own
# config.py variable, since it's always exactly HOSTINFO_CSV_PATH's own
# directory + "/history", never independently configurable.
HOSTINFO_HISTORY_DIR = os.path.join(os.path.dirname(config.HOSTINFO_CSV_PATH), "history")
HOSTINFO_SNAPSHOT_RE = re.compile(r"^hostinfo_([0-9]{8}_[0-9]{6})\.csv$")

# IdM Health tab (roles/idm_healthchecks' idm_hc_dashboard_* feed) - like
# Config Files below, a separate feature shown as one more tab on the
# per-host page, but UNLIKE Config Files this tab is only added for a
# given host when that host actually has data (see host_has_idm_healthcheck()
# and render_host_detail()) - most hosts in a fleet aren't IdM servers, so
# always showing an empty "IdM Health" tab everywhere would just be clutter.
IDM_HEALTHCHECK_CHECK_KEY = "idm_healthcheck"
# getattr(...) fallback for the same reason as CONFIG_SNAPSHOT_DATA_DIR above -
# an existing install's config.py may not have been re-templated yet.
IDM_HEALTHCHECK_DATA_DIR = getattr(config, "IDM_HEALTHCHECK_DATA_DIR", "/linuxdb/idm_healthcheck")
# ipa-healthcheck's own result values, mapped onto this app's three-tier
# STATUS_CLASS/badge system. ERROR is folded in with CRITICAL here - both
# are what roles/idm_healthchecks' idm_hc_fail_on_error already treats as
# the "stop everything" tier, so the dashboard badge should agree with it.
# Anything unrecognised (a future ipa-healthcheck result value this app
# doesn't know about yet) is treated as WARNING rather than silently PASS,
# so a new result type is never invisible on the Overview traffic light.
IDM_HEALTHCHECK_RESULT_STATUS = {
    "SUCCESS": "PASS",
    "WARNING": "WARNING",
    "ERROR": "CRITICAL",
    "CRITICAL": "CRITICAL",
}

# Combined tab bar: every linuxdb_reports check, plus Config Files at the
# end. A plain list (not inserted into config.CHECKS itself) so nothing
# about the existing checks' behaviour or ordering changes.
ALL_TABS = list(config.CHECKS) + [(CONFIG_FILES_CHECK_KEY, "Config Files")]
VALID_CHECK_KEYS.add(CONFIG_FILES_CHECK_KEY)
CHECK_LABELS[CONFIG_FILES_CHECK_KEY] = "Config Files"
# IdM Health is deliberately NOT added to ALL_TABS (see comment above) -
# it's still added to VALID_CHECK_KEYS/CHECK_LABELS so a direct ?check=
# link (or a bookmark from when the host did have data) still validates
# and renders its own "no data yet" notice instead of silently falling
# back to the first linuxdb_reports tab.
VALID_CHECK_KEYS.add(IDM_HEALTHCHECK_CHECK_KEY)
CHECK_LABELS[IDM_HEALTHCHECK_CHECK_KEY] = "IdM Health"

STATUS_CLASS = {
    "PASS": "status-pass",
    "OK": "status-pass",
    "INFO": "status-info",
    "WARNING": "status-warn",
    "WARN": "status-warn",
    "CRITICAL": "status-crit",
    "CRIT": "status-crit",
}

REPORT_FONT_STACK = "Consolas, 'Courier New', 'Liberation Mono', monospace"

# Overview-table columns that are always a short number (CPU/RAM/swap counts)
# and read better centered + narrow rather than left-aligned + wide. Matched
# against the hostinfo.csv header text (see group_vars/all/host_checks.yml),
# so this keeps working even if checks get reordered - it's not tied to a
# fixed column position.
NUM_COL_HEADERS = frozenset(("CPUS", "MEM_GB", "SWAP_GB"))

# hostinfo.csv columns that are a named service's up/down state (a plain
# "systemctl is-active <svc>" or equivalent - see group_vars/all/
# host_checks.yml) rather than a fact/setting - a value other than the
# one listed here as "healthy" means that service isn't running, which
# belongs on the Overview traffic light exactly like any other finding
# below. RSM222/RSM232/FLEXLM only run on hosts listed in their own
# site_hosts.yml list, so they're blank (and therefore skipped, same as
# any other inapplicable column) everywhere else.
# To add another such column (e.g. the commented-out POSTGRESQL_STATUS
# example in host_checks.yml), just add one more tuple here.
SERVICE_STATUS_COLUMNS = (
    ("CHRONYD", frozenset(("active",)), "Chronyd"),
    ("SSHD", frozenset(("active",)), "SSHD"),
    ("FIREWALLD", frozenset(("active",)), "firewalld"),
    ("AUDITD", frozenset(("active",)), "auditd"),
    ("CROND", frozenset(("active",)), "crond"),
    ("RSM222", frozenset(("active",)), "RSM222 service"),
    ("RSM232", frozenset(("active",)), "RSM232 service"),
    ("FLEXLM", frozenset(("running",)), "FlexLM license manager"),
)


def _norm_header(h):
    """Upper-cases and swaps spaces for underscores, so matching below
    works whichever way a given hostinfo.csv happens to spell a column
    ("DNF STATUS" vs "DNF_STATUS", etc)."""
    return h.strip().upper().replace(" ", "_")


def overview_row_status(header, row, disk_status=None, idm_status=None):
    """
    Traffic-light status for one Overview table row: looks at whichever
    of a small set of well-known hostinfo.csv columns actually exist in
    this row (different environments' host_checks.yml can enable
    different columns) and returns the worst finding as
    (status_key, reasons_list) - status_key is one of "CRITICAL",
    "WARNING", "PASS" (matching STATUS_CLASS), reasons_list is a plain
    English list used for the badge's hover tooltip. Never raises - an
    unparsable/missing value for any single column is simply skipped
    rather than failing the whole row.

    disk_status, if given, is the (status_key, reason) tuple from
    disk_check_overview_status() for this row's host - folded into the
    same worst-finding logic as the hostinfo.csv columns below, so a
    filesystem-utilisation problem contributes to this row's badge too.

    idm_status, if given, is the (status_key, reason) tuple from
    idm_healthcheck_overview_status() for this row's host - folded in
    exactly the same way as disk_status, so an IdM/Kerberos/replication
    problem also contributes to this row's badge. None for the (common)
    case of a host with no IdM healthcheck dashboard data at all.

    Returns a THIRD element too: flagged_columns, a set of normalised
    header names whose own value in this row directly tripped a
    WARNING/CRITICAL finding - callers use this to highlight the
    offending cell itself (bold red), not just the row-level badge, so
    the problem is obvious even to someone who doesn't notice the badge
    or read its tooltip. disk_status/idm_status have no corresponding
    hostinfo.csv column, so neither ever appears in flagged_columns.
    """
    # An unreachable-host placeholder row: hostinfo.csv.j2 pads these out
    # to the SAME column count as a real row (all-blank FQDN/IP/OS/checks),
    # so it's not actually "short" - len(row) == len(header) here. Detect
    # it by every field after HOSTNAME being blank instead, which a real
    # host_report never produces (FQDN/IP/OS are always populated when a
    # host answers at all). Also keeps the original short-row check as a
    # defensive fallback in case a row is truncated for some other reason.
    if len(row) < len(header) or (row and all(not (v or "").strip() for v in row[1:])):
        # There's nothing else to check here, but a host that didn't
        # report at all is itself the clearest "go look at this one"
        # signal there is.
        return "CRITICAL", ["Host unreachable on last hostinfo run"], set()

    values = {_norm_header(h): v.strip() for h, v in zip(header, row)}

    reasons_critical = []
    reasons_warning = []
    flagged_columns = set()

    failed_cnt = values.get("FAILED_SVC_CNT", "")
    try:
        if int(failed_cnt) > 0:
            reasons_critical.append(failed_cnt + " failed service(s)")
            flagged_columns.add("FAILED_SVC_CNT")
    except ValueError:
        pass

    if values.get("REBOOT_REQD", "").lower() == "yes":
        reasons_warning.append("Reboot required")
        flagged_columns.add("REBOOT_REQD")

    dnf_status = values.get("DNF_STATUS", "")
    if dnf_status and dnf_status.upper() != "UP_TO_DATE":
        reasons_warning.append("Updates pending (" + dnf_status + ")")
        flagged_columns.add("DNF_STATUS")

    if values.get("NTP_SYNC", "").lower() == "no":
        reasons_warning.append("NTP not synced")
        flagged_columns.add("NTP_SYNC")

    if values.get("IN_SATELLITE", "").lower() == "no":
        reasons_warning.append("Not registered in Satellite")
        flagged_columns.add("IN_SATELLITE")

    if values.get("SELINUX", "").lower() == "disabled":
        reasons_warning.append("SELinux disabled")
        flagged_columns.add("SELINUX")

    for col, healthy_values, label in SERVICE_STATUS_COLUMNS:
        svc_val = values.get(col, "")
        if svc_val and svc_val.lower() not in healthy_values:
            reasons_warning.append(label + " not running (" + svc_val + ")")
            flagged_columns.add(col)

    if disk_status is not None:
        d_status, d_reason = disk_status
        if d_status == "CRITICAL":
            reasons_critical.append(d_reason)
        elif d_status == "WARNING":
            reasons_warning.append(d_reason)

    if idm_status is not None:
        i_status, i_reason = idm_status
        if i_status == "CRITICAL":
            reasons_critical.append(i_reason)
        elif i_status == "WARNING":
            reasons_warning.append(i_reason)

    if reasons_critical:
        return "CRITICAL", reasons_critical + reasons_warning, flagged_columns
    if reasons_warning:
        return "WARNING", reasons_warning, flagged_columns
    return "PASS", [], flagged_columns


PAGE_CSS = (
    """
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
}
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0;
       background: var(--bg); color: var(--text); }
header { background: var(--surface); padding: 16px 24px; border-bottom: 1px solid var(--border);
         display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
header h1 { margin: 0; font-size: 1.25rem; }
header h1 a { color: var(--text); text-decoration: none; }
/* Cross-app switcher (LinuxDB Dashboard / Log Search / Insights / Guide)
   reuses the .tabs/.tab pill styling below, just without the body's
   margin-bottom - it sits inline in the header's right-hand side. */
.tabs.app-switcher { margin: 0; }
main { padding: 24px 32px; max-width: none; margin: 0; text-align: left; }
table { border-collapse: collapse; width: 100%; margin-bottom: 20px; font-size: 0.9rem;
        text-align: left; }
th, td { border: 1px solid var(--border); padding: 5px 8px; text-align: left;
         vertical-align: top; }
th { background: var(--surface-alt); position: sticky; top: 0; }
tr:nth-child(even) td { background: var(--surface-alt); }
a { color: var(--accent); }
.summary-highlight { background: #fff6da; border: 1px solid #e6c453; border-left: 5px solid #d9a319;
                      border-radius: var(--radius-sm); padding: 10px 16px; margin-bottom: 16px;
                      font-size: 1.05rem; font-weight: 600; color: #5c4406; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
        box-shadow: var(--shadow-card); padding: 16px 20px; margin-bottom: 20px; text-align: left; }
/* The visible border turns this into an unmistakable "this is its own
   scrollable box" region instead of table content that merely looks
   like it trails off the page. Bottom corners are square (not
   rounded) because .custom-hscroll sits flush underneath, forming one
   continuous rounded box together with it - see below. */
.table-scroll { overflow-x: auto; overflow-y: auto; max-height: 72vh; margin-bottom: 0;
                border: 1px solid var(--border-strong); border-bottom: none;
                border-radius: var(--radius) var(--radius) 0 0; }
/* Native scrollbars are unreliable as a visibility cue - some
   platforms/browsers hide them entirely until you touch the box, and
   custom ::-webkit-scrollbar styling doesn't reliably override that
   everywhere (notably Safari on macOS). Hidden here on purpose; the
   .custom-hscroll bar rendered by the script at the bottom of this
   page (see TABLE_SCROLLBAR_JS) is what's actually always visible -
   this element still scrolls normally via wheel/trackpad/drag, it
   just doesn't rely on the browser's own scrollbar chrome to show
   that it can. */
.table-scroll { scrollbar-width: none; }
.table-scroll::-webkit-scrollbar { display: none; }
.scroll-hint { font-size: 0.78rem; color: var(--text-muted); margin: 0 0 6px 0; }
.scroll-hint svg { vertical-align: -2px; margin-right: 4px; }
.table-scroll table { width: auto; min-width: 100%; margin-bottom: 0;
                       font-size: 0.76rem; }
/* No wrapping - each column sizes to fit its own content (nowrap makes
   the browser size every column at its natural content width instead
   of forcing/shrinking to the container), so nothing wraps onto extra
   lines or breaks mid-word. The tradeoff is a wider table that needs
   more horizontal scrolling - accepted deliberately in favor of
   compact, single-line, easy-to-scan cells. */
.table-scroll th, .table-scroll td { white-space: nowrap; }
/* Short, mostly-numeric columns (CPU/RAM/swap counts): centered rather
   than left-aligned, since that reads better for a short number.
   Matched by column header text in render_home() rather than position,
   so this keeps working if checks are reordered in
   group_vars/all/host_checks.yml. */
.table-scroll th.num-col, .table-scroll td.num-col { text-align: center; }
/* Click-to-sort column headers (see TABLE_SORT_JS below). Only the
   Overview fleet table currently uses .table-scroll on a real <table>,
   so this selector doesn't need to be scoped any narrower. Every
   sortable header always shows a faint up/down chevron icon so a
   user glancing at the table can tell columns are clickable without
   having to hover or sort one first; it swaps to a single solid
   chevron, in the accent colour, on whichever column is actively
   sorted. Real inline SVGs (three per header, shown/hidden with
   plain display:none - see the sort-arrow markup in render_home()),
   not a Unicode arrow character: a stray Unicode glyph like \u21C5 or
   \u25B2 renders as a missing-glyph tofu box on systems without a
   symbol font installed (confirmed in headless/minimal Linux
   environments), where an SVG always renders identically. */
.table-scroll th.sortable { cursor: pointer; user-select: none; padding-right: 20px; }
.table-scroll th.sortable:hover { background: var(--accent-soft); }
.table-scroll th.sortable .sort-arrow { display: inline-block; margin-left: 4px; vertical-align: -1px; }
.table-scroll th.sortable .sort-arrow svg { display: none; color: var(--text-muted); opacity: 0.5; }
.table-scroll th.sortable .sort-arrow svg.arrow-idle { display: inline-block; }
.table-scroll th.sortable:hover .sort-arrow svg.arrow-idle { opacity: 0.9; }
.table-scroll th.sortable.sort-asc, .table-scroll th.sortable.sort-desc {
  background: var(--accent-soft); color: var(--accent); font-weight: 700;
  /* Inset box-shadow, not a border - a border would add to the cell's
     box height and make this one <th> taller/misaligned against its
     un-sorted neighbours in the same header row; box-shadow paints
     over the existing border without changing layout at all. */
  box-shadow: inset 0 -3px 0 var(--accent); }
.table-scroll th.sortable.sort-asc .sort-arrow svg.arrow-idle,
.table-scroll th.sortable.sort-desc .sort-arrow svg.arrow-idle { display: none; }
.table-scroll th.sortable.sort-asc .sort-arrow svg.arrow-asc,
.table-scroll th.sortable.sort-desc .sort-arrow svg.arrow-desc {
  display: inline-block; color: var(--accent); opacity: 1; }
/* Custom, always-on-screen replacement for the native scrollbar -
   inserted directly under each .table-scroll box by TABLE_SCROLLBAR_JS
   at the bottom of the page. Rendered and positioned entirely in CSS/
   JS rather than relying on any browser/OS scrollbar behaviour, so it
   looks and works identically everywhere, and is never hidden. */
.custom-hscroll { position: relative; height: 14px; background: var(--surface-alt);
                  border: 1px solid var(--border-strong); border-top: none;
                  border-radius: 0 0 var(--radius) var(--radius); margin-bottom: 6px; cursor: pointer; }
.custom-hscroll-thumb { position: absolute; top: 1px; left: 0; height: 10px;
                         background: #6b7178; border-radius: 6px; cursor: grab; }
.custom-hscroll-thumb:hover { background: #4d5257; }
.custom-hscroll-thumb.dragging { background: #4d5257; cursor: grabbing; }
/* Hidden rather than removed when a table happens to fit without
   scrolling, so layout doesn't jump if the window is resized wider
   then narrower again. */
.custom-hscroll.no-overflow { visibility: hidden; }
/* Freeze the first column (hostname) while scrolling right, so a wide row
   of columns never loses its "which host is this" anchor off-screen. */
.table-scroll th:first-child, .table-scroll td:first-child {
  position: sticky; left: 0; z-index: 2; max-width: none; white-space: nowrap;
  box-shadow: 2px 0 4px -2px rgba(0, 0, 0, 0.2);
}
.table-scroll th:first-child { background: var(--surface-alt); }
.table-scroll tr:nth-child(odd) td:first-child { background: var(--surface); }
.table-scroll tr:nth-child(even) td:first-child { background: var(--surface-alt); }
/* Overview table row tint for the traffic-light status computed in
   overview_row_status() - whole-row colour so a problem host stands out
   even before reading the STATUS badge itself. Listed after the
   nth-child(odd/even) sticky-column rules above (same specificity, so
   source order decides) so this wins for the sticky first column too -
   otherwise a tinted row's frozen HOSTNAME cell would stay white/grey
   while the rest of the row turned red/amber. Colour is never the only
   signal - see the CRIT/WARN text in the badge itself. */
.table-scroll tr.row-crit td, .table-scroll tr.row-crit td:first-child { background: var(--crit-bg); }
.table-scroll tr.row-warn td, .table-scroll tr.row-warn td:first-child { background: var(--warn-bg); }
/* Per-cell highlight for the specific value that tripped a WARNING/
   CRITICAL finding (see overview_row_status()'s flagged_columns return
   value) - bold red text plus its own background tint, so the exact
   offending value (e.g. "inactive" in a CHRONYD cell) stands out even
   to someone who doesn't notice the row tint or the badge/tooltip.
   Listed after the row-crit/row-warn rules above so it wins ties on
   source order - it needs to visibly differ from the lighter whole-row
   tint it usually sits inside of, not blend into it. Used on both the
   Overview table and the per-host detail table. */
.cell-alert, .table-scroll tr.row-crit td.cell-alert, .table-scroll tr.row-warn td.cell-alert {
  color: var(--crit); font-weight: 700; background: #f6c6c1;
}
/* Overview page "jump to a host" search box - lets someone type a
   hostname and go straight to its report menu (/host/<name>) instead
   of hunting for it in the (possibly long, sideways-scrolling) fleet
   table below. A real GET form (see render_home()/application()'s
   /goto-host route) so pressing Enter works with JS disabled too; the
   <datalist> of known hostnames is a pure progressive enhancement on
   top of that. Kept visually distinct from the plain .card boxes used
   elsewhere (soft accent tint + left accent bar) so it reads as a
   standalone "quick action" rather than another data panel. */
.host-jump { background: var(--accent-soft); border: 1px solid var(--border);
             border-left: 4px solid var(--accent); border-radius: var(--radius);
             padding: 14px 18px; margin-bottom: 20px; }
.host-jump-label { display: block; font-weight: 600; margin-bottom: 8px; }
.host-jump-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.host-jump-row svg { flex: 0 0 auto; color: var(--text-muted); }
.host-jump-row input[type="text"] { flex: 0 1 340px; min-width: 220px; max-width: 340px; }
.host-jump-hint { margin: 8px 0 0 0; font-size: 0.82rem; }
.tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
/* padding/font-size kept identical to log_search_ui's .tab rule on
   purpose - this pill is the same shared cross-app nav component
   (see _app_switcher_html()/render_nav()), so a button has to be the
   same physical size regardless of which app's page is rendering it. */
.tab { padding: 8px 16px; border-radius: var(--radius-sm); background: var(--surface);
       border: 1px solid var(--border); text-decoration: none; color: var(--text); font-weight: 600;
       font-size: 0.9rem; }
.tab.active { background: var(--accent); border-color: var(--accent); color: #fff; }
.tab:hover:not(.active) { border-color: var(--border-strong); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
         font-size: 0.8rem; font-weight: 600; }
.status-pass { background: var(--success-bg); color: var(--success); }
.status-info { background: var(--info-bg); color: var(--info); }
.status-warn { background: var(--warn-bg); color: var(--warn); }
.status-crit { background: var(--crit-bg); color: var(--crit); }
.status-default { background: #eceef1; color: #4a4e55; }
.muted { color: var(--text-muted); }
.report-output, .report-output table, .report-output pre {
  font-family: """
    + REPORT_FONT_STACK
    + """;
}
/* Per-host detail page (render_host_detail): the value column (each
   <td>, e.g. IP, KERNEL, SERIAL) in monospace so hostinfo.csv values
   line up and are easy to copy-paste accurately - the label column
   (<th>) stays in the regular body font since it's prose, not data. */
.host-info-table td {
  font-family: """
    + REPORT_FONT_STACK
    + """;
}
/* Whole-row highlight for a flagged field on the per-host detail page
   (e.g. NTP_SYNC: no) - .cell-alert above already turns the value
   itself bold red with a pink background; this matches the label cell
   to the exact same bold red text + pink background so both columns
   of the row are highlighted identically, not just the value. */
.host-info-table tr.row-alert th {
  color: var(--crit); font-weight: 700; background: #f6c6c1;
}
/* On even rows the generic "tr:nth-child(even) td { background:
   #fafafa; }" striping rule above is more specific than the plain
   .cell-alert rule, so it was silently winning and leaving the value
   cell's pink background missing (text stayed red, background did
   not) whenever a flagged field landed on an even row. Re-assert the
   pink here with a selector specific enough to beat the striping. */
.host-info-table tr.row-alert td.cell-alert {
  background: #f6c6c1;
}
pre { background: var(--surface-alt); border: 1px solid var(--border); border-radius: var(--radius-sm);
      padding: 12px 14px; overflow-x: auto; white-space: pre-wrap;
      word-break: break-word; font-size: 0.85rem; text-align: left; }
.notice { background: var(--surface-alt); border-left: 3px solid var(--accent); padding: 10px 14px;
          margin-bottom: 16px; text-align: left; }
.error { border-left-color: var(--crit); }
select, button, input[type="text"] { background: var(--surface); color: var(--text); border: 1px solid var(--border);
                  border-radius: var(--radius-sm); padding: 6px 10px; font-size: 0.92rem; }
input[type="text"]:focus, select:focus { outline: none; border-color: var(--accent);
                  box-shadow: 0 0 0 3px var(--accent-soft); }
button { background: var(--accent); color: #fff; border-color: var(--accent); cursor: pointer; font-weight: 600; }
button:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
form.history { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.rc-ok { color: var(--success); }
.rc-warn { color: var(--warn); }
.rc-bad { color: var(--crit); }
/* Pre-rendered Operational Support Guide (see render_guide() and
   docs/render_guide.py) - the markdown source already uses its own
   heading hierarchy (h1/h2/h3), so these are scoped to .guide-content
   only rather than changing every other page's headings. Fluid width
   (fills the window like the rest of the app, capped only so lines
   don't stretch to an unreadable length on ultra-wide monitors) rather
   than a fixed box - it now shrinks/grows with the browser window
   instead of sitting at a fixed size regardless of viewport. */
.guide-content { max-width: min(100%, 1400px); width: 100%; }
.guide-content h1 { font-size: 1.6rem; margin-top: 0; }
.guide-content h2 { font-size: 1.3rem; margin-top: 2em; padding-top: 0.8em;
                     border-top: 1px solid var(--border); }
.guide-content h2:first-child { margin-top: 0; padding-top: 0; border-top: none; }
.guide-content h3 { font-size: 1.08rem; margin-top: 1.6em; }
.guide-content ul, .guide-content ol { padding-left: 1.4em; }
.guide-content li { margin-bottom: 0.3em; }
.guide-content table { font-size: 0.85rem; }
.guide-content hr { border: none; border-top: 1px solid var(--border); margin: 2em 0; }
/* Inline `code` - a subtle chip so it reads as code without competing
   with the .report-output/.host-info-table monospace treatment used
   elsewhere. Suppressed again inside <pre> (fenced blocks) since the
   generic `pre` rule above already boxes the whole block - a nested
   inline-code box on top of that would double up the chrome. */
.guide-content code { font-family: """ + REPORT_FONT_STACK + """;
                       background: var(--surface-alt); border: 1px solid var(--border);
                       border-radius: 4px; padding: 1px 5px; font-size: 0.85em; }
.guide-content pre code { background: none; border: none; padding: 0; }
/* Build-time freshness stamp written by docs/render_guide.py. Kept
   deliberately small and faint - it's metadata, not content. */
.guide-footer { margin-top: 1.5em; padding-top: 0.6em; border-top: 1px solid var(--border); color: var(--text-faint); font-size: 0.78rem; }
"""
)

def _app_switcher_html(current):
    """Cross-app navigation switcher shown in the header of every page -
    LinuxDB Dashboard's own pages (Overview, host detail, IdM Health,
    Package Search) and /guide all get the same bar, so any page can jump
    straight to any of the five destinations, in this fixed order:
    LinuxDB Dashboard, Log Search, Insights, Package Search, Guide - all
    navigating in the same tab. Log Search / Insights only appear once
    the sibling app URL is configured (see
    linuxdb_dashboard_sibling_app_name/_url in defaults/main.yml) - this
    stays additive, same as the old sibling-link behaviour: an
    unconfigured deployment just shows LinuxDB Dashboard, Package Search
    and Guide, exactly as before this feature existed for the other two
    tabs. Package Search is always placed right after Log
    Search/Insights (whether or not they're actually present) and right
    before Guide, so its position in the bar never shifts depending on
    whether the sibling app is configured. getattr() also guards against
    an older, not-yet-redeployed config.py that predates the sibling
    settings.

    `current` is "linuxdb", "packages", or "guide" - whichever page this
    is gets rendered active (highlighted, non-navigating-away) instead
    of linked.
    """
    def cls(name):
        return "tab active" if name == current else "tab"

    items = ['<a class="{0}" href="/">LinuxDB Dashboard</a>'.format(cls("linuxdb"))]

    sibling_name = getattr(config, "SIBLING_APP_NAME", "")
    sibling_url = getattr(config, "SIBLING_APP_URL", "")
    if sibling_name and sibling_url:
        items.append(
            '<a class="tab" href="{url}">{name}</a>'.format(
                url=html.escape(sibling_url), name=html.escape(sibling_name)
            )
        )
        insights_url = sibling_url.rstrip("/") + "/insights"
        items.append(
            '<a class="tab" href="{url}">Insights</a>'.format(url=html.escape(insights_url))
        )

    items.append('<a class="{0}" href="/packages">Package Search</a>'.format(cls("packages")))
    items.append('<a class="{0}" href="/guide">Guide</a>'.format(cls("guide")))

    return '<nav class="tabs app-switcher">' + "".join(items) + "</nav>"


# Pre-rendered from OPERATIONAL_SUPPORT_GUIDE.md at build time by
# docs/render_guide.py, deployed alongside app.py by this role's own
# tasks/main.yml ("Deploy the pre-rendered operational guide"). Cached
# in memory but re-read whenever the file's mtime/size changes, so a
# freshly copied guide_content.html takes effect on the next /guide
# page view - no service restart needed. The stat() check is two
# syscalls per request on a read-only app, so it's negligible. A
# missing file (e.g. an older deploy that predates this feature)
# degrades to an empty string rather than raising - render_guide()
# below turns that into a friendly notice instead of a 500.
GUIDE_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide_content.html")

# Cache state for _load_guide_html(). Guarded only by the GIL - a
# double-read after a concurrent mtime flip is harmless (same content).
_GUIDE_CACHE = {"key": None, "html": ""}


def _load_guide_html():
    try:
        st = os.stat(GUIDE_HTML_PATH)
        key = (st.st_mtime_ns, st.st_size)
        if key != _GUIDE_CACHE["key"]:
            with open(GUIDE_HTML_PATH, "r", encoding="utf-8") as fh:
                html = fh.read()
            _GUIDE_CACHE["key"] = key
            _GUIDE_CACHE["html"] = html
    except OSError:
        _GUIDE_CACHE["key"] = None
        _GUIDE_CACHE["html"] = ""
    return _GUIDE_CACHE["html"]


PAGE_HEAD = (
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title>LinuxDB Dashboard</title>\n"
    "<style>" + PAGE_CSS + "</style>\n</head>\n<body>\n"
    "<header><h1><a href=\"/\">LinuxDB Dashboard</a></h1>" + _app_switcher_html("linuxdb") + "</header>\n<main>\n"
)

# Separate head for /guide: even though it now navigates in the same
# tab (no target=_blank anywhere in the switcher), its tab title and
# on-page heading still need to say "Operational Support Guide" rather
# than reusing PAGE_HEAD's "LinuxDB Dashboard" - otherwise the browser
# tab title (and the <h1>) would misleadingly say "LinuxDB Dashboard"
# while showing the guide. The <h1> still links back to "/" (this is
# still the same app/host), it just reads correctly for the page you're
# actually on.
GUIDE_PAGE_HEAD = (
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title>Operational Support Guide</title>\n"
    "<style>" + PAGE_CSS + "</style>\n</head>\n<body>\n"
    "<header><h1><a href=\"/\">Operational Support Guide</a></h1>"
    + _app_switcher_html("guide")
    + "</header>\n<main>\n"
)

# Separate head for /packages, same reasoning as GUIDE_PAGE_HEAD above -
# its own tab title/<h1> rather than reusing PAGE_HEAD's "LinuxDB
# Dashboard" wording.
PACKAGES_PAGE_HEAD = (
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title>Package Search - LinuxDB Dashboard</title>\n"
    "<style>" + PAGE_CSS + "</style>\n</head>\n<body>\n"
    "<header><h1><a href=\"/\">Package Search</a></h1>"
    + _app_switcher_html("packages")
    + "</header>\n<main>\n"
)

# Renders a custom, always-visible horizontal scrollbar under every
# .table-scroll box (see PAGE_CSS .custom-hscroll comment for why the
# native browser scrollbar is hidden instead of relied on). Pure
# vanilla JS, no dependencies - runs once per page load, finds every
# .table-scroll element, and wires up a synced draggable bar for it.
# Scrolling itself still works natively (wheel/trackpad/drag) even
# with JS disabled; only the visible bar/thumb need it.
TABLE_SCROLLBAR_JS = """
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.table-scroll').forEach(function (box) {
    var bar = document.createElement('div');
    bar.className = 'custom-hscroll';
    var thumb = document.createElement('div');
    thumb.className = 'custom-hscroll-thumb';
    bar.appendChild(thumb);
    box.parentNode.insertBefore(bar, box.nextSibling);

    function layout() {
      var ratio = box.clientWidth / box.scrollWidth;
      if (ratio >= 1) {
        bar.classList.add('no-overflow');
        return;
      }
      bar.classList.remove('no-overflow');
      thumb.style.width = Math.max(ratio * bar.clientWidth, 30) + 'px';
      sync();
    }

    function sync() {
      var maxScroll = box.scrollWidth - box.clientWidth;
      var maxThumb = bar.clientWidth - thumb.offsetWidth;
      var pos = maxScroll > 0 ? (box.scrollLeft / maxScroll) * maxThumb : 0;
      thumb.style.left = pos + 'px';
    }

    box.addEventListener('scroll', sync);
    window.addEventListener('resize', layout);
    layout();

    var dragging = false;
    var startX = 0;
    var startLeft = 0;

    thumb.addEventListener('mousedown', function (e) {
      dragging = true;
      startX = e.clientX;
      startLeft = thumb.offsetLeft;
      thumb.classList.add('dragging');
      e.preventDefault();
    });
    document.addEventListener('mousemove', function (e) {
      if (!dragging) { return; }
      var maxThumb = bar.clientWidth - thumb.offsetWidth;
      var newLeft = Math.min(Math.max(0, startLeft + (e.clientX - startX)), maxThumb);
      thumb.style.left = newLeft + 'px';
      var maxScroll = box.scrollWidth - box.clientWidth;
      box.scrollLeft = maxThumb > 0 ? (newLeft / maxThumb) * maxScroll : 0;
    });
    document.addEventListener('mouseup', function () {
      dragging = false;
      thumb.classList.remove('dragging');
    });

    bar.addEventListener('click', function (e) {
      if (e.target === thumb) { return; }
      var rect = bar.getBoundingClientRect();
      var clickX = e.clientX - rect.left;
      var maxThumb = bar.clientWidth - thumb.offsetWidth;
      var newLeft = Math.min(Math.max(0, clickX - thumb.offsetWidth / 2), maxThumb);
      thumb.style.left = newLeft + 'px';
      var maxScroll = box.scrollWidth - box.clientWidth;
      box.scrollLeft = maxThumb > 0 ? (newLeft / maxThumb) * maxScroll : 0;
    });
  });
});
"""

# Three small inline SVGs per sortable header, toggled with plain CSS
# display:none/inline-block (see .sort-arrow rules in PAGE_CSS) -
# deliberately not a Unicode arrow character/CSS ::after content, since
# a glyph like \u21C5/\u25B2 renders as a missing-glyph tofu box on
# systems without a symbol font installed; an inline SVG always renders
# the same regardless of installed fonts. Shared by every sortable
# .table-scroll table on the dashboard (Overview's fleet table and
# Package Search's results table both use this same markup).
SORT_ARROW_HTML = (
    "<span class=\"sort-arrow\" aria-hidden=\"true\">"
    "<svg class=\"arrow-idle\" width=\"11\" height=\"11\" viewBox=\"0 0 24 24\" "
    "fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.5\" "
    "stroke-linecap=\"round\" stroke-linejoin=\"round\">"
    "<polyline points=\"8 10 12 6 16 10\"></polyline>"
    "<polyline points=\"8 14 12 18 16 14\"></polyline></svg>"
    "<svg class=\"arrow-asc\" width=\"11\" height=\"11\" viewBox=\"0 0 24 24\" "
    "fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.5\" "
    "stroke-linecap=\"round\" stroke-linejoin=\"round\">"
    "<polyline points=\"6 15 12 9 18 15\"></polyline></svg>"
    "<svg class=\"arrow-desc\" width=\"11\" height=\"11\" viewBox=\"0 0 24 24\" "
    "fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.5\" "
    "stroke-linecap=\"round\" stroke-linejoin=\"round\">"
    "<polyline points=\"6 9 12 15 18 9\"></polyline></svg>"
    "</span>"
)

# Click-a-<th>-to-sort for any sortable .table-scroll table - the
# Overview fleet table and Package Search's results table both use
# this (see the matching CSS comment above .table-scroll th.sortable).
# Pure vanilla JS, no dependency on the CSV/backend: it just reorders
# <tr> elements already in the DOM, so row classes (row-crit/row-warn),
# the sticky first column, and the status badge/link inside it all move with
# their row untouched.
#
# Sort key per column:
#   - num-col headers (CPUS/MEM_GB/SWAP_GB - see NUM_COL_HEADERS):
#     parsed as a float; unparsable/blank cells sort to the bottom of
#     an ascending sort (treated as -Infinity) rather than throwing.
#   - every other column: the cell's own text, or - for the HOSTNAME
#     column specifically - the <a> link's text alone, so sorting by
#     hostname isn't thrown off by the OK/WARN/CRIT badge text that
#     sits in the same cell. Compared with localeCompare(numeric:true)
#     for a natural sort ("host2" before "host10").
TABLE_SORT_JS = """
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.table-scroll table').forEach(function (table) {
    var headerRow = table.rows[0];
    if (!headerRow) { return; }
    var ths = Array.prototype.slice.call(headerRow.cells);
    var sortState = { col: -1, dir: 1 };

    function sortValue(row, idx, numeric) {
      var cell = row.cells[idx];
      if (!cell) { return numeric ? -Infinity : ''; }
      var link = cell.querySelector('a');
      var text = (link ? link.textContent : cell.textContent).trim();
      if (numeric) {
        var n = parseFloat(text.replace(/[^0-9.-]/g, ''));
        return isNaN(n) ? -Infinity : n;
      }
      return text.toLowerCase();
    }

    ths.forEach(function (th, idx) {
      if (!th.classList.contains('sortable')) { return; }
      th.addEventListener('click', function () {
        var numeric = th.classList.contains('num-col');
        var dir = (sortState.col === idx) ? -sortState.dir : 1;
        sortState = { col: idx, dir: dir };

        ths.forEach(function (h) { h.classList.remove('sort-asc', 'sort-desc'); });
        th.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');

        var bodyRows = Array.prototype.slice.call(table.rows).slice(1);
        bodyRows.sort(function (a, b) {
          var av = sortValue(a, idx, numeric);
          var bv = sortValue(b, idx, numeric);
          if (numeric) {
            return (av - bv) * dir;
          }
          return av.localeCompare(bv, undefined, { numeric: true, sensitivity: 'base' }) * dir;
        });
        var parent = headerRow.parentNode;
        bodyRows.forEach(function (row) { parent.appendChild(row); });
      });
    });
  });
});
"""

PAGE_TAIL = (
    "<script>" + TABLE_SCROLLBAR_JS + "\n" + TABLE_SORT_JS + "</script>\n"
    "</main>\n</body>\n</html>\n"
)


def _e(value):
    """Shorthand: escape anything before it goes into HTML."""
    return html.escape("" if value is None else str(value))


def _safe_join(base, *parts):
    """
    Join base with parts, but only return a path if it resolves to
    somewhere still under base. Returns None if any part would escape
    base (e.g. via '..' or a symlink) - the caller must treat None as
    "not found", never fall back to an unsafe path.
    """
    base_real = os.path.realpath(base)
    candidate = os.path.realpath(os.path.join(base, *parts))
    if candidate == base_real or candidate.startswith(base_real + os.sep):
        return candidate
    return None


def is_valid_hostname(name):
    # HOSTNAME_RE alone allows a bare ".." (both characters are in its
    # allowed set), which would otherwise be treated as a literal,
    # syntactically "valid" hostname by every regex-based check in this
    # file. It's not caught here because it's exploitable - _safe_join()
    # independently blocks any path that would actually escape the data
    # directory - but a hostname of "." or ".." isn't a sane value and
    # should be rejected up front rather than silently falling through
    # to a confusing "no data for this host" page.
    return bool(name) and bool(HOSTNAME_RE.match(name)) and ".." not in name and name not in (".", "..")


def is_valid_timestamp(ts):
    return bool(ts) and bool(TIMESTAMP_RE.match(ts))


def is_valid_config_slug(slug):
    # Same extra guard as is_valid_hostname() above and for the same
    # reason: CONFIG_FILE_SLUG_RE's charset allows "." (needed for real
    # slugs like pam.d), which also makes a bare "." or ".." technically
    # match. _safe_join() alone would resolve either of those to the data
    # directory itself rather than escaping it, but neither is a sane
    # slug value, so reject them explicitly up front.
    return (
        bool(slug)
        and bool(CONFIG_FILE_SLUG_RE.match(slug))
        and ".." not in slug
        and slug not in (".", "..")
    )


###############################################################################
# hostinfo.csv parsing
###############################################################################

def parse_hostinfo(path=None):
    """
    Parses the multi-section file roles/hostinfo writes:
      1. a pipe-delimited table (header + one row per reachable/attempted host)
      2. a blank line, then "SUMMARY" and KEY=VALUE lines
      3. a blank line, then "UNREACHABLE_HOST_LIST" and one hostname per line

    Reads config.HOSTINFO_CSV_PATH (the live file) by default, or an
    explicit `path` - used to render an archived snapshot from
    HOSTINFO_HISTORY_DIR instead (see render_home()).

    Returns (header, rows, summary_dict, unreachable_list, error_str).
    On any problem, header/rows/summary/unreachable are empty and
    error_str is set to a human-readable message (never an exception).
    """
    target_path = path or config.HOSTINFO_CSV_PATH
    try:
        with open(target_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except FileNotFoundError:
        if path:
            return [], [], {}, [], "Snapshot file no longer exists: " + target_path
        return [], [], {}, [], (
            "No hostinfo.csv found at " + config.HOSTINFO_CSV_PATH
            + " - run playbooks/hostinfo.yml first."
        )
    except OSError as exc:
        return [], [], {}, [], "Could not read hostinfo.csv: " + str(exc)

    blocks = content.split("\n\n")
    table_lines = [ln for ln in blocks[0].splitlines() if ln.strip()]
    if not table_lines:
        return [], [], {}, [], "hostinfo.csv is empty."

    header = table_lines[0].split("|")
    rows = [ln.split("|") for ln in table_lines[1:]]
    # Always show hosts in alphabetical order by HOSTNAME (case-insensitive),
    # regardless of whatever order hostinfo.yml happened to process them in.
    rows.sort(key=lambda r: r[0].strip().lower() if r else "")

    summary = {}
    unreachable = []
    for block in blocks[1:]:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if lines[0].strip() == "SUMMARY":
            for ln in lines[1:]:
                if "=" in ln:
                    k, v = ln.split("=", 1)
                    summary[k.strip()] = v.strip()
        elif lines[0].strip() == "UNREACHABLE_HOST_LIST":
            unreachable = [ln.strip() for ln in lines[1:] if ln.strip()]

    return header, rows, summary, unreachable, None


def find_hostinfo_row(header, rows, hostname):
    """Case-insensitive lookup of a row by its HOSTNAME (first) column."""
    if not header:
        return None
    target = hostname.lower()
    for row in rows:
        if row and row[0].strip().lower() == target:
            return row
    return None


def format_epoch(epoch):
    """Epoch seconds -> "YYYY-MM-DD HH:MM:SS" in the server's local time,
    or None if epoch isn't a usable number (caller shows a fallback)."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def hostinfo_last_updated():
    """Last-modified time of the live hostinfo.csv, formatted for display,
    or None if the file doesn't exist / isn't stat-able."""
    try:
        return format_epoch(os.path.getmtime(config.HOSTINFO_CSV_PATH))
    except OSError:
        return None


def list_hostinfo_history():
    """
    Sorted (newest first) list of timestamps ('YYYYMMDD_HHMMSS' strings)
    for every archived hostinfo_<timestamp>.csv snapshot roles/hostinfo
    has saved under HOSTINFO_HISTORY_DIR. Capped at config.MAX_HISTORY_LIST,
    same limit used for the per-check history dropdowns.
    """
    try:
        fnames = os.listdir(HOSTINFO_HISTORY_DIR)
    except OSError:
        return []
    out = []
    for fname in fnames:
        m = HOSTINFO_SNAPSHOT_RE.match(fname)
        if m:
            out.append(m.group(1))
    return sorted(out, reverse=True)[: config.MAX_HISTORY_LIST]


def hostinfo_snapshot_path(timestamp):
    """Validated, traversal-safe path to a snapshot file for `timestamp`,
    or None if the timestamp is malformed or the file doesn't exist."""
    if not is_valid_timestamp(timestamp):
        return None
    candidate = _safe_join(HOSTINFO_HISTORY_DIR, "hostinfo_" + timestamp + ".csv")
    if candidate and os.path.isfile(candidate):
        return candidate
    return None


###############################################################################
# linuxdb_reports history
###############################################################################

def list_hosts_with_reports():
    """Hostnames (directory names) that have any linuxdb_reports data at all."""
    try:
        return sorted(
            d for d in os.listdir(config.LINUXDB_REPORTS_DATA_DIR)
            if os.path.isdir(os.path.join(config.LINUXDB_REPORTS_DATA_DIR, d))
        )
    except OSError:
        return []


def host_exists(hostname):
    """True if `hostname` is a known asset - it has a row in the live
    (non-snapshot) hostinfo.csv, or it has report history under
    LINUXDB_REPORTS_DATA_DIR - i.e. it's the same set of names offered by
    the "Jump to a host" datalist. Used by /goto-host to tell a real,
    just-not-yet-reported-on host (still worth landing on /host/<name> to
    show the "collect it with..." hint) apart from a made-up/typo'd
    hostname that isn't any known host at all (which should say so
    instead of opening what looks like a real host's empty page)."""
    if hostname in list_hosts_with_reports():
        return True
    _header, rows, _summary, _unreachable, _error = parse_hostinfo()
    return any(r and r[0].strip() == hostname for r in rows)


def list_check_timestamps(hostname, check_key):
    """Sorted (newest first) list of timestamps available for hostname/check_key."""
    if not is_valid_hostname(hostname) or check_key not in VALID_CHECK_KEYS:
        return []
    check_dir = _safe_join(config.LINUXDB_REPORTS_DATA_DIR, hostname, check_key)
    if not check_dir or not os.path.isdir(check_dir):
        return []
    out = []
    try:
        for fname in os.listdir(check_dir):
            if fname.endswith(".txt"):
                ts = fname[:-4]
                if is_valid_timestamp(ts):
                    out.append(ts)
    except OSError:
        return []
    return sorted(out, reverse=True)


def resolve_latest_timestamp(hostname, check_key):
    check_dir = _safe_join(config.LINUXDB_REPORTS_DATA_DIR, hostname, check_key)
    if check_dir:
        latest_path = os.path.join(check_dir, "latest")
        try:
            with open(latest_path, "r", encoding="utf-8") as fh:
                ts = fh.read().strip()
            if is_valid_timestamp(ts):
                return ts
        except OSError:
            pass
    timestamps = list_check_timestamps(hostname, check_key)
    return timestamps[0] if timestamps else None


def read_check_output(hostname, check_key, timestamp):
    """Returns (content_or_None, rc_or_None, error_or_None)."""
    if not is_valid_hostname(hostname):
        return None, None, "Invalid hostname."
    if check_key not in VALID_CHECK_KEYS:
        return None, None, "Unknown check."
    if not is_valid_timestamp(timestamp):
        return None, None, "Invalid timestamp."

    txt_path = _safe_join(config.LINUXDB_REPORTS_DATA_DIR, hostname, check_key, timestamp + ".txt")
    if not txt_path or not os.path.isfile(txt_path):
        return None, None, "No data for that run."

    try:
        with open(txt_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return None, None, "Could not read report: " + str(exc)

    rc = None
    rc_path = _safe_join(config.LINUXDB_REPORTS_DATA_DIR, hostname, check_key, timestamp + ".rc")
    if rc_path and os.path.isfile(rc_path):
        try:
            with open(rc_path, "r", encoding="utf-8", errors="replace") as fh:
                rc = fh.read().strip()
        except OSError:
            rc = None

    return content, rc, None


def disk_check_overview_status(hostname):
    """
    (status_key, reason) for hostname's most recent df_check run, or
    None if there's no df_check data for this host at all, or that run
    was clean (rc 0). Feeds into overview_row_status() so a filesystem
    utilisation problem (WARN_THRESHOLD/CRIT_THRESHOLD in df_check.sh)
    also shows up in the Overview page's own traffic light, not only
    on its Disk & Filesystem tab. df_check.sh exits 0=PASS/1=WARNING/
    2=CRITICAL; rc_class() already treats anything other than 0/1 as
    the worst bucket, so any unexpected code is read as CRITICAL here
    too rather than silently ignored.
    """
    if "df_check" not in VALID_CHECK_KEYS or not is_valid_hostname(hostname):
        return None
    ts = resolve_latest_timestamp(hostname, "df_check")
    if not ts:
        return None
    _content, rc, _err = read_check_output(hostname, "df_check", ts)
    if rc is None:
        return None
    try:
        code = int(rc)
    except ValueError:
        return None
    if code == 1:
        return "WARNING", "Disk/filesystem check reported WARNING (see Disk & Filesystem tab)"
    if code != 0:
        return "CRITICAL", "Disk/filesystem check reported CRITICAL (see Disk & Filesystem tab)"
    return None


###############################################################################
# Fleet-wide package search (backs /packages - see application() below)
###############################################################################
# rpm_report.sh always emits a fixed HOSTNAME|PACKAGE|VERSION|RELEASE|ARCH|
# INSTALLED layout (see that script's own "Output Format" comment) - column
# positions are hardcoded here the same way render_check_body() already
# hardcodes svr_check.sh/dnf_updates.sh's own HOSTNAME|CHECK|STATUS|VALUE
# layout, rather than trying to discover columns generically at runtime.
RPM_REPORT_PACKAGE_COL = 1
RPM_REPORT_VERSION_COL = 2
RPM_REPORT_RELEASE_COL = 3
RPM_REPORT_ARCH_COL = 4


def search_installed_packages(query):
    """
    Case-insensitive substring search for `query` across every host's
    latest rpm_report run - the data behind the "Package Search" page:
    type "firefox", get back every host that has it installed, with the
    exact version/release/arch on each. Reads only already-collected
    data (the same files backing each host's own Installed Packages
    tab) - no new collection mechanism, no live rpm query against any
    managed host.

    Returns a list of (hostname, package, version, release, arch,
    run_timestamp) tuples, sorted by hostname then package name.
    Returns [] for a blank query, or if "rpm_report" isn't a configured
    check at all - so an older/unconfigured deployment just shows an
    empty-results Package Search page instead of raising.
    """
    query = (query or "").strip()
    if not query or "rpm_report" not in VALID_CHECK_KEYS:
        return []
    needle = query.lower()
    results = []
    for hostname in list_hosts_with_reports():
        if not is_valid_hostname(hostname):
            continue
        ts = resolve_latest_timestamp(hostname, "rpm_report")
        if not ts:
            continue
        content, _rc, _err = read_check_output(hostname, "rpm_report", ts)
        if not content:
            continue
        lines = [ln for ln in content.splitlines() if ln.strip()]
        header, rows, _consumed = parse_pipe_block(lines)
        if not header:
            continue
        for row in rows:
            if len(row) <= RPM_REPORT_PACKAGE_COL:
                continue
            if needle in row[RPM_REPORT_PACKAGE_COL].lower():
                results.append((
                    hostname,
                    row[RPM_REPORT_PACKAGE_COL],
                    row[RPM_REPORT_VERSION_COL] if len(row) > RPM_REPORT_VERSION_COL else "",
                    row[RPM_REPORT_RELEASE_COL] if len(row) > RPM_REPORT_RELEASE_COL else "",
                    row[RPM_REPORT_ARCH_COL] if len(row) > RPM_REPORT_ARCH_COL else "",
                    ts,
                ))
    results.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    return results


###############################################################################
# IdM healthcheck history (roles/idm_healthchecks' idm_hc_dashboard_* feed)
###############################################################################

def list_idm_healthcheck_hosts():
    """
    Hostnames (directory names) that have any IdM healthcheck dashboard
    data at all. Purely data-driven - no dependency on any inventory
    group name (e.g. "idm") - so this naturally covers homelab's single
    IdM server or work's six replicated ones without any code change.
    Used for both the Overview page's fleet-wide "IdM Health" summary
    and to decide which hosts get a per-host "IdM Health" tab at all.
    """
    try:
        return sorted(
            d for d in os.listdir(IDM_HEALTHCHECK_DATA_DIR)
            if os.path.isdir(os.path.join(IDM_HEALTHCHECK_DATA_DIR, d))
        )
    except OSError:
        return []


def list_idm_healthcheck_timestamps(hostname):
    """Sorted (newest first) list of run timestamps available for hostname."""
    if not is_valid_hostname(hostname):
        return []
    host_dir = _safe_join(IDM_HEALTHCHECK_DATA_DIR, hostname)
    if not host_dir or not os.path.isdir(host_dir):
        return []
    out = []
    try:
        for fname in os.listdir(host_dir):
            if fname.endswith(".json"):
                ts = fname[:-5]
                if is_valid_timestamp(ts):
                    out.append(ts)
    except OSError:
        return []
    return sorted(out, reverse=True)


def resolve_idm_healthcheck_latest(hostname):
    """Same 'latest' pointer-file-with-fallback pattern as resolve_latest_timestamp()."""
    host_dir = _safe_join(IDM_HEALTHCHECK_DATA_DIR, hostname)
    if host_dir:
        latest_path = os.path.join(host_dir, "latest")
        try:
            with open(latest_path, "r", encoding="utf-8") as fh:
                ts = fh.read().strip()
            if is_valid_timestamp(ts):
                return ts
        except OSError:
            pass
    timestamps = list_idm_healthcheck_timestamps(hostname)
    return timestamps[0] if timestamps else None


def host_has_idm_healthcheck(hostname):
    """
    Whether hostname has any IdM healthcheck dashboard data at all - drives
    whether the per-host "IdM Health" tab is shown at all. Unlike the
    always-shown Config Files tab, this tab would be pure clutter on the
    majority of hosts in a fleet that aren't IdM servers.
    """
    if not is_valid_hostname(hostname):
        return False
    return bool(resolve_idm_healthcheck_latest(hostname))


def read_idm_healthcheck(hostname, timestamp):
    """
    Returns (entries_or_None, error_or_None). entries is the parsed
    ipa-healthcheck JSON array roles/idm_healthchecks wrote (its own
    idm_hc_parsed fact) - a list of dicts shaped like
    {"source": ..., "check": ..., "result": "SUCCESS"|"WARNING"|
    "ERROR"|"CRITICAL", "kw": {"msg": ..., ...}, ...}, exactly
    ipa-healthcheck's own --output-type=json schema.
    """
    if not is_valid_hostname(hostname):
        return None, "Invalid hostname."
    if not is_valid_timestamp(timestamp):
        return None, "Invalid timestamp."
    json_path = _safe_join(IDM_HEALTHCHECK_DATA_DIR, hostname, timestamp + ".json")
    if not json_path or not os.path.isfile(json_path):
        return None, "No data for that run."
    try:
        with open(json_path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, "Could not read IdM healthcheck data: " + str(exc)
    try:
        entries = json.loads(raw)
    except ValueError:
        return None, "IdM healthcheck data is not valid JSON."
    if not isinstance(entries, list):
        return None, "Unexpected IdM healthcheck data shape (expected a list)."
    return entries, None


def idm_healthcheck_overview_status(hostname):
    """
    (status_key, reason) for hostname's most recent IdM healthcheck run,
    or None if there's no IdM healthcheck data for this host at all, or
    that run was entirely SUCCESS. Feeds into overview_row_status() the
    same way disk_check_overview_status() does for df_check, so an IdM/
    Kerberos/replication problem also shows up on the Overview page's own
    traffic light, not only on the host's IdM Health tab. Deliberately
    has no dependency on any particular inventory group name - it only
    looks at whether dashboard data actually exists for this hostname.
    """
    if not is_valid_hostname(hostname):
        return None
    ts = resolve_idm_healthcheck_latest(hostname)
    if not ts:
        return None
    entries, error = read_idm_healthcheck(hostname, ts)
    if error or entries is None:
        return None

    critical_cnt = 0
    warning_cnt = 0
    for entry in entries:
        result = str((entry or {}).get("result", "")).upper()
        status = IDM_HEALTHCHECK_RESULT_STATUS.get(result, "WARNING" if result != "SUCCESS" else "PASS")
        if status == "CRITICAL":
            critical_cnt += 1
        elif status == "WARNING":
            warning_cnt += 1

    if critical_cnt:
        return "CRITICAL", (
            str(critical_cnt) + " IdM healthcheck issue(s) at ERROR/CRITICAL (see IdM Health tab)"
        )
    if warning_cnt:
        return "WARNING", (
            str(warning_cnt) + " IdM healthcheck issue(s) at WARNING (see IdM Health tab)"
        )
    return None


def render_idm_healthcheck_section(hostname, params):
    """
    Renders the per-host "IdM Health" tab body: ipa-healthcheck's own
    source/check/result/msg entries as a colour-coded table, reusing the
    same STATUS_CLASS/render_status_badge conventions as everywhere else
    in this app. Mirrors render_config_files_section()'s run-history
    dropdown, but this tab is only ever linked to from render_host_detail()
    when host_has_idm_healthcheck(hostname) is true - reaching here with
    no data (e.g. a stale bookmark from before data was pruned) is handled
    below exactly like any other now-missing run rather than erroring.
    """
    timestamps = list_idm_healthcheck_timestamps(hostname)[: config.MAX_HISTORY_LIST]
    requested_ts = params.get("ts", [""])[0]
    if requested_ts and is_valid_timestamp(requested_ts) and requested_ts in timestamps:
        active_ts = requested_ts
    else:
        active_ts = resolve_idm_healthcheck_latest(hostname)

    if not active_ts:
        return (
            "<div class=\"notice\">No IdM healthcheck data yet for this host. Collect it with:"
            "<br><code>ansible-playbook playbooks/idm_healthcheck.yml --limit "
            + _e(hostname) + "</code></div>"
        )

    out = []
    if timestamps:
        out.append("<form class=\"history\" method=\"get\" action=\"/host/" + quote(hostname) + "\">")
        out.append("<input type=\"hidden\" name=\"check\" value=\"" + _e(IDM_HEALTHCHECK_CHECK_KEY) + "\">")
        out.append("<label for=\"ts\">Run:</label>")
        out.append("<select name=\"ts\" id=\"ts\">")
        for ts in timestamps:
            sel = " selected" if ts == active_ts else ""
            out.append("<option value=\"" + _e(ts) + "\"" + sel + ">" + _e(ts) + "</option>")
        out.append("</select>")
        out.append("<button type=\"submit\">View</button>")
        out.append("</form>")

    entries, error = read_idm_healthcheck(hostname, active_ts)
    if error:
        out.append("<div class=\"notice error\">" + _e(error) + "</div>")
        return "".join(out)

    total = len(entries)
    issues = []
    critical_cnt = 0
    warning_cnt = 0
    for entry in entries:
        entry = entry or {}
        result = str(entry.get("result", "")).upper()
        status = IDM_HEALTHCHECK_RESULT_STATUS.get(result, "WARNING" if result != "SUCCESS" else "PASS")
        if status == "CRITICAL":
            critical_cnt += 1
            issues.append((entry, result, status))
        elif status == "WARNING":
            warning_cnt += 1
            issues.append((entry, result, status))
    success_cnt = total - critical_cnt - warning_cnt

    out.append(
        "<p class=\"muted\">Run " + _e(active_ts) + " &nbsp;|&nbsp; " + str(total) + " check(s): "
        + str(success_cnt) + " passed, " + str(warning_cnt) + " warning(s), "
        + str(critical_cnt) + " error/critical</p>"
    )

    if not issues:
        out.append("<div class=\"notice\">All IdM healthcheck checks passed on this run.</div>")
    else:
        out.append("<table><tr><th>Source</th><th>Check</th><th>Result</th><th>Message</th></tr>")
        for entry, result, status in issues:
            kw = entry.get("kw")
            msg = kw.get("msg", "") if isinstance(kw, dict) else ""
            out.append("<tr>")
            out.append("<td>" + _e(entry.get("source", "")) + "</td>")
            out.append("<td>" + _e(entry.get("check", "")) + "</td>")
            out.append("<td>" + render_status_badge(result, status) + "</td>")
            out.append("<td>" + _e(msg) + "</td>")
            out.append("</tr>")
        out.append("</table>")

    return "".join(out)


###############################################################################
# config_snapshot history (roles/config_snapshot)
###############################################################################

def list_config_slugs(hostname):
    """Sorted list of per-file slug directories with config_snapshot data for hostname."""
    if not is_valid_hostname(hostname):
        return []
    host_dir = _safe_join(CONFIG_SNAPSHOT_DATA_DIR, hostname)
    if not host_dir or not os.path.isdir(host_dir):
        return []
    try:
        return sorted(
            d for d in os.listdir(host_dir)
            if CONFIG_FILE_SLUG_RE.match(d) and os.path.isdir(os.path.join(host_dir, d))
        )
    except OSError:
        return []


def read_config_path(hostname, slug):
    """Reads the 'path' sidecar file recording the real source path for a slug."""
    if not is_valid_hostname(hostname) or not is_valid_config_slug(slug):
        return None
    path_file = _safe_join(CONFIG_SNAPSHOT_DATA_DIR, hostname, slug, "path")
    if not path_file or not os.path.isfile(path_file):
        return None
    try:
        with open(path_file, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read().strip()
        return content or None
    except OSError:
        return None


def list_config_timestamps(hostname, slug):
    """
    Sorted (newest first) list of run timestamps recorded for hostname/slug -
    covers .txt (file captured), .sha256, and .missing (file didn't exist
    at that run) so a run where the file was absent still shows up in history.
    """
    if not is_valid_hostname(hostname) or not is_valid_config_slug(slug):
        return []
    slug_dir = _safe_join(CONFIG_SNAPSHOT_DATA_DIR, hostname, slug)
    if not slug_dir or not os.path.isdir(slug_dir):
        return []
    out = set()
    try:
        for fname in os.listdir(slug_dir):
            for suffix in (".txt", ".sha256", ".missing"):
                if fname.endswith(suffix):
                    ts = fname[: -len(suffix)]
                    if is_valid_timestamp(ts):
                        out.add(ts)
                    break
    except OSError:
        return []
    return sorted(out, reverse=True)


def resolve_latest_config_timestamp(hostname, slug):
    slug_dir = _safe_join(CONFIG_SNAPSHOT_DATA_DIR, hostname, slug)
    if slug_dir:
        latest_path = os.path.join(slug_dir, "latest")
        try:
            with open(latest_path, "r", encoding="utf-8") as fh:
                ts = fh.read().strip()
            if is_valid_timestamp(ts):
                return ts
        except OSError:
            pass
    timestamps = list_config_timestamps(hostname, slug)
    return timestamps[0] if timestamps else None


def read_config_snapshot(hostname, slug, timestamp):
    """
    Returns (content_or_None, is_missing, sha256_or_None, error_or_None).
    content is always None whenever is_missing is True or error is set.
    """
    if not is_valid_hostname(hostname):
        return None, False, None, "Invalid hostname."
    if not is_valid_config_slug(slug):
        return None, False, None, "Unknown config file."
    if not is_valid_timestamp(timestamp):
        return None, False, None, "Invalid timestamp."

    missing_path = _safe_join(CONFIG_SNAPSHOT_DATA_DIR, hostname, slug, timestamp + ".missing")
    if missing_path and os.path.isfile(missing_path):
        return None, True, None, None

    txt_path = _safe_join(CONFIG_SNAPSHOT_DATA_DIR, hostname, slug, timestamp + ".txt")
    if not txt_path or not os.path.isfile(txt_path):
        return None, False, None, "No data for that run."

    try:
        with open(txt_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return None, False, None, "Could not read config snapshot: " + str(exc)

    sha256 = None
    sha_path = _safe_join(CONFIG_SNAPSHOT_DATA_DIR, hostname, slug, timestamp + ".sha256")
    if sha_path and os.path.isfile(sha_path):
        try:
            with open(sha_path, "r", encoding="utf-8", errors="replace") as fh:
                sha256 = fh.read().strip() or None
        except OSError:
            sha256 = None

    return content, False, sha256, None


def classify_change(hostname, slug):
    """
    Compares the two most recent runs for hostname/slug and returns
    (label, status_key) describing the LATEST run - status_key is fed
    straight into the existing STATUS_CLASS badge lookup (PASS/INFO/
    WARNING/CRITICAL, falling back to a neutral grey badge for anything
    else), so this reuses the exact same badge CSS as every other check
    with no new styling.
    """
    timestamps = list_config_timestamps(hostname, slug)
    if not timestamps:
        return "No data", "UNKNOWN"

    latest = timestamps[0]
    _content, latest_missing, latest_sha, _err = read_config_snapshot(hostname, slug, latest)

    if len(timestamps) < 2:
        return ("Missing", "UNKNOWN") if latest_missing else ("First run", "INFO")

    previous = timestamps[1]
    _pcontent, prev_missing, prev_sha, _perr = read_config_snapshot(hostname, slug, previous)

    if latest_missing and prev_missing:
        return "Missing", "UNKNOWN"
    if latest_missing and not prev_missing:
        return "Now missing", "CRITICAL"
    if prev_missing and not latest_missing:
        return "File appeared", "WARNING"
    if latest_sha and prev_sha:
        return ("Changed", "WARNING") if latest_sha != prev_sha else ("Unchanged", "PASS")
    # sha256 sidecar unavailable for one of the two runs - no proof either
    # way, so say so explicitly rather than guessing "unchanged".
    return "Unknown", "UNKNOWN"


def render_status_badge(label, status_key, title=None):
    cls = STATUS_CLASS.get(status_key.upper(), "status-default")
    title_attr = " title=\"" + _e(title) + "\"" if title else ""
    return "<span class=\"badge " + cls + "\"" + title_attr + ">" + _e(label) + "</span>"


def render_config_files_list(hostname):
    slugs = list_config_slugs(hostname)
    if not slugs:
        return (
            "<div class=\"notice\">No config file snapshots yet for this host. Collect them with:"
            "<br><code>ansible-playbook playbooks/linuxdb_dashboard.yml --tags config_snapshot --limit "
            + _e(hostname) + "</code></div>"
        )

    entries = []
    for slug in slugs:
        path = read_config_path(hostname, slug) or slug
        latest_ts = resolve_latest_config_timestamp(hostname, slug)
        label, status_key = classify_change(hostname, slug)
        entries.append((path, slug, latest_ts, label, status_key))
    entries.sort(key=lambda e: e[0].lower())

    out = ["<table><tr><th>Config file</th><th>Status</th><th>Last captured</th></tr>"]
    for path, slug, latest_ts, label, status_key in entries:
        link = (
            "/host/" + quote(hostname) + "?check=" + quote(CONFIG_FILES_CHECK_KEY)
            + "&file=" + quote(slug)
        )
        out.append("<tr><td><a href=\"" + link + "\">" + _e(path) + "</a></td>")
        out.append("<td>" + render_status_badge(label, status_key) + "</td>")
        out.append("<td>" + _e(latest_ts if latest_ts else "-") + "</td></tr>")
    out.append("</table>")
    return "".join(out)


def render_config_file_detail(hostname, slug, params):
    path = read_config_path(hostname, slug)
    if path is None:
        return "<div class=\"notice error\">Unknown config file.</div>"

    list_link = "/host/" + quote(hostname) + "?check=" + quote(CONFIG_FILES_CHECK_KEY)
    out = ["<p><a href=\"" + list_link + "\">&larr; All config files</a></p>"]
    out.append("<h3>" + _e(path) + "</h3>")

    label, status_key = classify_change(hostname, slug)
    out.append("<p>" + render_status_badge(label, status_key) + "</p>")

    timestamps = list_config_timestamps(hostname, slug)[: config.MAX_HISTORY_LIST]
    requested_ts = params.get("ts", [""])[0]
    if requested_ts and is_valid_timestamp(requested_ts) and requested_ts in timestamps:
        active_ts = requested_ts
    else:
        active_ts = resolve_latest_config_timestamp(hostname, slug)

    if not active_ts:
        out.append("<div class=\"notice\">No data captured yet for this file.</div>")
        return "".join(out)

    if timestamps:
        out.append("<form class=\"history\" method=\"get\" action=\"/host/" + quote(hostname) + "\">")
        out.append("<input type=\"hidden\" name=\"check\" value=\"" + _e(CONFIG_FILES_CHECK_KEY) + "\">")
        out.append("<input type=\"hidden\" name=\"file\" value=\"" + _e(slug) + "\">")
        out.append("<label for=\"ts\">Run:</label>")
        out.append("<select name=\"ts\" id=\"ts\">")
        for ts in timestamps:
            sel = " selected" if ts == active_ts else ""
            out.append("<option value=\"" + _e(ts) + "\"" + sel + ">" + _e(ts) + "</option>")
        out.append("</select>")
        out.append("<button type=\"submit\">View</button>")
        out.append("</form>")

    content, is_missing, sha256, error = read_config_snapshot(hostname, slug, active_ts)
    if error:
        out.append("<div class=\"notice error\">" + _e(error) + "</div>")
    elif is_missing:
        out.append("<div class=\"notice\">File did not exist on this host at this run.</div>")
    else:
        meta = "Run " + _e(active_ts)
        if sha256:
            meta += " &nbsp;|&nbsp; sha256: <code>" + _e(sha256) + "</code>"
        out.append("<p class=\"muted\">" + meta + "</p>")
        out.append('<div class="report-output"><pre>' + _e(content) + "</pre></div>")

    return "".join(out)


def render_config_files_section(hostname, params):
    requested_slug = params.get("file", [""])[0]
    if (
        requested_slug
        and is_valid_config_slug(requested_slug)
        and requested_slug in list_config_slugs(hostname)
    ):
        return render_config_file_detail(hostname, requested_slug, params)
    return render_config_files_list(hostname)


###############################################################################
# Rendering helpers
###############################################################################

def parse_pipe_block(lines):
    """
    Given a list of non-blank lines where the first is a pipe-delimited
    header, parse contiguous following lines that also contain '|' as
    rows. Returns (header_list, rows_list, consumed_line_count).
    """
    if not lines or "|" not in lines[0]:
        return None, [], 0
    header = [c.strip() for c in lines[0].split("|")]
    rows = []
    consumed = 1
    for ln in lines[1:]:
        if "|" not in ln:
            break
        rows.append([c.strip() for c in ln.split("|")])
        consumed += 1
    return header, rows, consumed


def render_table(
    header, rows, status_col_name=None,
    match_col_name=None, match_values=None, highlight_col_name=None,
):
    """
    match_col_name/match_values/highlight_col_name are an optional,
    independent way to bold-red-highlight one cell per row (same
    .cell-alert class the Overview/host-detail pages use) - any row
    whose match_col_name cell's value is in match_values gets its
    highlight_col_name cell flagged. Used by df_check's leading
    utilisation table: match_col_name="MOUNTPOINT", match_values=the
    set of mountpoints already called out as WARNING/CRITICAL in the
    "FILESYSTEM UTILISATION WARNINGS" section beneath it, highlight_col_
    name="USE%" - so the actual over-threshold percentage is bold red
    right in the table, not just named in the plain-text block below.
    """
    out = ["<table>", "<tr>"]
    for h in header:
        out.append("<th>" + _e(h) + "</th>")
    out.append("</tr>")
    status_idx = header.index(status_col_name) if status_col_name in header else None
    match_idx = header.index(match_col_name) if match_col_name in header else None
    highlight_idx = header.index(highlight_col_name) if highlight_col_name in header else None
    for row in rows:
        out.append("<tr>")
        row_flagged = (
            match_idx is not None
            and match_values
            and match_idx < len(row)
            and row[match_idx].strip() in match_values
        )
        for idx, cell in enumerate(row):
            if idx == status_idx:
                cls = STATUS_CLASS.get(cell.strip().upper(), "status-default")
                out.append("<td><span class=\"badge " + cls + "\">" + _e(cell) + "</span></td>")
            elif row_flagged and idx == highlight_idx:
                out.append("<td class=\"cell-alert\">" + _e(cell) + "</td>")
            else:
                out.append("<td>" + _e(cell) + "</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def render_check_body(check_key, content):
    """
    Renders one check's raw output. Well-known formats get a parsed
    table (svr_check/dnf_updates's single flat HOSTNAME|CHECK|STATUS|
    VALUE table; df_check/proc_check/system_stats's leading table plus
    raw remainder); anything else - including future/custom checks not
    recognised here - safely falls back to preformatted raw text, which
    is always correct even if not as pretty.
    """
    lines = content.splitlines()
    non_blank_from_start = []
    for ln in lines:
        if ln.strip() == "" and non_blank_from_start:
            break
        if ln.strip():
            non_blank_from_start.append(ln)

    if check_key in ("svr_check", "dnf_updates"):
        header, rows, consumed = parse_pipe_block(non_blank_from_start)
        if header:
            return render_table(header, rows, status_col_name="STATUS")
        return "<pre>" + _e(content) + "</pre>"

    if check_key in ("df_check", "proc_check", "system_stats"):
        header, rows, consumed = parse_pipe_block(non_blank_from_start)
        if header:
            remainder_lines = lines[consumed:]
            # Drop the single blank separator line right after the table.
            if remainder_lines and remainder_lines[0].strip() == "":
                remainder_lines = remainder_lines[1:]
            remainder = "\n".join(remainder_lines).strip("\n")

            flagged_mounts = set()
            if check_key == "df_check":
                # Pull the mountpoints already identified as WARNING/
                # CRITICAL in the "FILESYSTEM UTILISATION WARNINGS"
                # section just below this table (df_check.sh emits
                # CRITICAL|mountpoint|pct%|fs / WARNING|mountpoint|pct%|
                # fs there) so the leading table's own USE% cell for
                # that mountpoint can be bold-red highlighted too, not
                # just named in the plain-text block underneath.
                for ln in remainder_lines:
                    parts = ln.split("|")
                    if len(parts) >= 2 and parts[0].strip().upper() in ("WARNING", "CRITICAL"):
                        flagged_mounts.add(parts[1].strip())

            if flagged_mounts:
                table_html = render_table(
                    header, rows,
                    match_col_name="MOUNTPOINT",
                    match_values=flagged_mounts,
                    highlight_col_name="USE%",
                )
            else:
                table_html = render_table(header, rows)

            extra = "<pre>" + _e(remainder) + "</pre>" if remainder else ""
            return table_html + extra
        return "<pre>" + _e(content) + "</pre>"

    if check_key == "rpm_report":
        # Plain HOSTNAME|PACKAGE|VERSION|RELEASE|ARCH|INSTALLED table, no
        # STATUS column - unlike the checks above, this is an inventory
        # listing, not a threshold-based check, so there's nothing to
        # colour-code (see rpm_report.sh's own "Output Format" comment).
        header, rows, _consumed = parse_pipe_block(non_blank_from_start)
        if header:
            return render_table(header, rows)
        return "<pre>" + _e(content) + "</pre>"

    return "<pre>" + _e(content) + "</pre>"


def rc_class(rc):
    if rc is None:
        return "muted"
    try:
        code = int(rc)
    except ValueError:
        return "muted"
    if code == 0:
        return "rc-ok"
    if code == 1:
        return "rc-warn"
    return "rc-bad"


###############################################################################
# Pages
###############################################################################

def _csv_export_filename(params):
    """hostinfo_<timestamp>.csv - the archived snapshot's own timestamp if
    ?snapshot= was a valid one, otherwise \"now\" for a live-file export."""
    requested_snapshot = (params.get("snapshot", [""])[0] or "").strip()
    if requested_snapshot and is_valid_timestamp(requested_snapshot):
        return "hostinfo_" + requested_snapshot + ".csv"
    return "hostinfo_" + time.strftime("%Y%m%d_%H%M%S") + ".csv"


def render_overview_csv(params):
    """
    Same data as the Overview table (live hostinfo.csv, or an archived
    snapshot via ?snapshot=<timestamp>), as a plain .csv download - so
    the fleet table can be dropped straight into a spreadsheet without
    copy/pasting out of the browser.
    """
    requested_snapshot = (params.get("snapshot", [""])[0] or "").strip()
    snapshot_path = hostinfo_snapshot_path(requested_snapshot) if requested_snapshot else None
    header, rows, _summary, _unreachable, error = parse_hostinfo(snapshot_path)

    buf = io.StringIO()
    writer = csv.writer(buf)
    if header:
        writer.writerow(header)
        for row in rows:
            writer.writerow(row + [""] * (len(header) - len(row)))
    elif error:
        writer.writerow(["error"])
        writer.writerow([error])
    return buf.getvalue()


def render_host_jump(known_hosts, search_error, return_to=None, heading="Jump to a host&rsquo;s reports", not_found_error=None):
    """"Jump to a host" search box - types a hostname and goes straight to
    its report menu (/host/<name>) without scrolling/hunting for it in a
    table. Shown at the top of the Overview page, and again on every
    per-host report page so you can jump straight to a *different* host
    without backing out to "All hosts" first. Submits a real GET to
    /goto-host (see application()'s routing), which 302s to
    /host/<hostname> after validating it AND confirming host_exists() -
    so this works with JS disabled too, not just as a client-side
    convenience. `known_hosts` (a sorted iterable of hostnames already on
    this page - fleet table rows plus "Other hosts with report history")
    backs a <datalist> so typing offers real-time suggestions/autocomplete.

    `return_to`, if given, is stamped on as a hidden field so that an
    *invalid or unrecognized* hostname bounces the user back to the page
    they searched from (e.g. another host's report page) instead of
    always landing on the Overview page - see the /goto-host route, which
    only ever honours a same-site path here, never a full URL.

    `search_error` (set when the typed value fails the charset check) and
    `not_found_error` (set when it's syntactically fine but isn't a known
    host - not in hostinfo.csv and no report history) render two distinct
    notices; only one is ever set at a time by /goto-host.
    """
    out = ["<form class=\"host-jump\" method=\"get\" action=\"/goto-host\" role=\"search\">"]
    out.append("<label class=\"host-jump-label\" for=\"host-jump-input\">" + heading + "</label>")
    if return_to:
        out.append("<input type=\"hidden\" name=\"return_to\" value=\"" + _e(return_to) + "\">")
    out.append("<div class=\"host-jump-row\">")
    out.append(
        "<svg width=\"18\" height=\"18\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" "
        "stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\">"
        "<circle cx=\"11\" cy=\"11\" r=\"7\"></circle><line x1=\"21\" y1=\"21\" x2=\"16.65\" y2=\"16.65\"></line></svg>"
    )
    out.append(
        "<input type=\"text\" id=\"host-jump-input\" name=\"hostname\" list=\"host-jump-list\" "
        "placeholder=\"e.g. idm1.lab.example.com\" autocomplete=\"off\" autocapitalize=\"off\" "
        "spellcheck=\"false\" required>"
    )
    out.append("<datalist id=\"host-jump-list\">")
    for h in known_hosts:
        out.append("<option value=\"" + _e(h) + "\"></option>")
    out.append("</datalist>")
    out.append("<button type=\"submit\">View reports</button>")
    out.append("</div>")
    out.append(
        "<p class=\"muted host-jump-hint\">Type a hostname and press Enter to open its "
        "report menu directly.</p>"
    )
    if search_error:
        out.append(
            "<div class=\"notice error\" style=\"margin: 10px 0 0 0;\">&ldquo;" + _e(search_error)
            + "&rdquo; doesn't look like a valid hostname &mdash; letters, numbers, dots and hyphens only.</div>"
        )
    elif not_found_error:
        out.append(
            "<div class=\"notice error\" style=\"margin: 10px 0 0 0;\">&ldquo;" + _e(not_found_error)
            + "&rdquo; &mdash; hostname not found. Check the spelling and try again.</div>"
        )
    out.append("</form>")
    return "".join(out)


def render_home(params):
    history = list_hostinfo_history()
    requested_snapshot = (params.get("snapshot", [""])[0] or "").strip()
    snapshot_path = hostinfo_snapshot_path(requested_snapshot) if requested_snapshot else None
    viewing_snapshot = bool(snapshot_path)

    header, rows, summary, unreachable, error = parse_hostinfo(snapshot_path)
    # Deliberately not wrapped in the bordered .card panel used elsewhere -
    # the hostinfo.csv table can be wide, and the card's border used to
    # visually overlap/clip through it. The heading and summary keep
    # their own light styling (.summary-highlight) without a hard border.
    body = ["<h2>LinuxDB Overview</h2>"]

    known_hosts = sorted(set(r[0].strip() for r in rows if r and r[0].strip()) | set(list_hosts_with_reports()))
    search_error = (params.get("host_search_error", [""])[0] or "").strip()
    not_found_error = (params.get("host_not_found", [""])[0] or "").strip()
    body.append(render_host_jump(known_hosts, search_error, not_found_error=not_found_error))

    if viewing_snapshot:
        body.append(
            "<div class=\"notice\">Viewing archived snapshot from " + _e(requested_snapshot)
            + " &nbsp;|&nbsp; <a href=\"/\">View latest</a></div>"
        )
    elif requested_snapshot:
        # A snapshot was asked for but didn't validate/exist any more
        # (e.g. pruned since the link was bookmarked) - fall through to
        # the live view rather than a dead end, but say so.
        body.append(
            "<div class=\"notice error\">Snapshot \"" + _e(requested_snapshot)
            + "\" not found - showing the latest hostinfo.csv instead.</div>"
        )

    if not viewing_snapshot:
        last_updated = hostinfo_last_updated()
        if last_updated:
            body.append("<p class=\"muted\">hostinfo.csv last updated: <strong>"
                         + _e(last_updated) + "</strong></p>")

    if history:
        body.append("<form class=\"history\" method=\"get\" action=\"/\">")
        body.append("<label for=\"snapshot\">History:</label>")
        body.append("<select name=\"snapshot\" id=\"snapshot\">")
        body.append("<option value=\"\">Latest (live)</option>")
        for ts in history:
            sel = " selected" if ts == requested_snapshot and viewing_snapshot else ""
            body.append("<option value=\"" + _e(ts) + "\"" + sel + ">" + _e(ts) + "</option>")
        body.append("</select>")
        body.append("<button type=\"submit\">View</button>")
        body.append("</form>")

    # Exports whichever hostinfo.csv is currently being viewed - the live
    # file, or the archived snapshot selected via the history dropdown
    # above - as a plain .csv download (see render_overview_csv()).
    csv_link = "/export.csv"
    if viewing_snapshot:
        csv_link += "?snapshot=" + quote(requested_snapshot)
    # &#8681; prefix + "Download ... as CSV" wording is shared with the
    # Insights page's CSV links (roles/log_search_ui) - keep both in sync
    # if either wording ever changes.
    body.append("<p><a href=\"" + csv_link + "\">&#8681; Download this table as CSV</a></p>")

    if error:
        body.append("<div class=\"notice error\">" + _e(error) + "</div>")
    else:
        if summary:
            parts = []
            for key in ("TOTAL_HOSTS", "REACHABLE_HOSTS", "UNREACHABLE_HOSTS"):
                if key in summary:
                    parts.append(_e(key) + ": <strong>" + _e(summary[key]) + "</strong>")
            if parts:
                # Highlighted so it isn't missed above the table - a
                # plain <p> here was too easy to skip past on first look.
                body.append("<div class=\"summary-highlight\">" + " &nbsp;|&nbsp; ".join(parts) + "</div>")

        if unreachable:
            # Shown right under the summary counters (not after the table)
            # so it's seen immediately alongside UNREACHABLE_HOSTS, rather
            # than only after scrolling past the whole fleet table. One
            # host per line rather than comma-separated - with more than
            # one down host a comma-run is easy to misread as a single
            # long hostname.
            body.append(
                "<div class=\"notice error\"><strong>Unreachable on last hostinfo run:</strong><ul>"
                + "".join("<li>" + _e(h) + "</li>" for h in unreachable)
                + "</ul></div>"
            )

        # Fleet-wide IdM Health summary - only rendered at all if
        # IDM_HEALTHCHECK_DATA_DIR actually has data for at least one host.
        # Purely data-driven (list_idm_healthcheck_hosts()/
        # idm_healthcheck_overview_status() have no group-name assumption),
        # so this naturally scales from homelab's single IdM server to
        # work's six replicated ones with no code change either way.
        idm_hosts = [h for h in list_idm_healthcheck_hosts() if is_valid_hostname(h)]
        if idm_hosts:
            idm_crit_hosts = []
            idm_warn_hosts = []
            for h in idm_hosts:
                idm_status = idm_healthcheck_overview_status(h)
                if idm_status is None:
                    continue
                if idm_status[0] == "CRITICAL":
                    idm_crit_hosts.append(h)
                elif idm_status[0] == "WARNING":
                    idm_warn_hosts.append(h)

            def _host_links(hosts):
                return ", ".join(
                    "<a href=\"/host/" + quote(h) + "\">" + _e(h) + "</a>" for h in hosts
                )

            if idm_crit_hosts or idm_warn_hosts:
                problem_cnt = len(idm_crit_hosts) + len(idm_warn_hosts)
                notice_cls = "notice error" if idm_crit_hosts else "notice"
                lines = [
                    "<strong>IdM Health:</strong> " + str(problem_cnt) + " of " + str(len(idm_hosts))
                    + " IdM server(s) reporting issues."
                ]
                if idm_crit_hosts:
                    lines.append("<strong>CRITICAL:</strong> " + _host_links(idm_crit_hosts))
                if idm_warn_hosts:
                    lines.append("<strong>WARNING:</strong> " + _host_links(idm_warn_hosts))
                body.append("<div class=\"" + notice_cls + "\">" + " &nbsp;|&nbsp; ".join(lines) + "</div>")
            else:
                body.append(
                    "<div class=\"notice\"><strong>IdM Health:</strong> All " + str(len(idm_hosts))
                    + " IdM server(s) passing (" + _host_links(idm_hosts) + ")</div>"
                )

        if not rows:
            body.append("<p class=\"muted\">No hosts in hostinfo.csv yet.</p>")
        else:
            # Wrapped in a borderless, scrollable pane (rather than the
            # .card border used elsewhere) so a wide/tall hostinfo.csv
            # scrolls within one screen instead of overflowing past the
            # card's rounded border - that overflow is what made the
            # table look like it was "poking through" the border before.
            # Monospace matches the report-output styling used on the
            # per-check pages, since this table is effectively rendering
            # the raw hostinfo.csv grid.
            # The HOSTNAME cell itself is the "view details" link (there's no
            # separate leading "Reports" column any more) - it's also the
            # sticky, frozen-while-scrolling first column (see .table-scroll
            # th:first-child / td:first-child in PAGE_CSS), so whichever host's
            # row you're looking at stays on screen no matter how far right
            # you scroll through the rest of hostinfo.csv's columns.
            body.append(
                "<p class=\"scroll-hint\">"
                "<svg width=\"13\" height=\"13\" viewBox=\"0 0 24 24\" fill=\"none\" "
                "stroke=\"currentColor\" stroke-width=\"2.5\" stroke-linecap=\"round\" "
                "stroke-linejoin=\"round\"><polyline points=\"7 7 2 12 7 17\"></polyline>"
                "<polyline points=\"17 7 22 12 17 17\"></polyline></svg>"
                "This table scrolls sideways for more columns "
                "&mdash; drag the table or use the scroll bar below it.</p>"
            )
            body.append(
                "<p class=\"scroll-hint\">"
                "<svg width=\"13\" height=\"13\" viewBox=\"0 0 24 24\" fill=\"none\" "
                "stroke=\"currentColor\" stroke-width=\"2.5\" stroke-linecap=\"round\" "
                "stroke-linejoin=\"round\"><polyline points=\"7 14 12 19 17 14\"></polyline>"
                "<polyline points=\"7 10 12 5 17 10\"></polyline></svg>"
                "Click any column heading to sort by it "
                "&mdash; click again to reverse the order.</p>"
            )
            body.append("<div class=\"table-scroll report-output\"><table><tr>")
            for col_idx, h in enumerate(header):
                th_classes = ["sortable"]
                if h.strip().upper() in NUM_COL_HEADERS:
                    th_classes.append("num-col")
                cls = " class=\"" + " ".join(th_classes) + "\""
                body.append(
                    "<th" + cls + " data-col=\"" + str(col_idx) + "\">" + _e(h)
                    + SORT_ARROW_HTML + "</th>"
                )
            body.append("</tr>")
            for row in rows:
                hostname = row[0].strip() if row else ""
                # Traffic-light: worst finding across this row's own
                # columns (failed services, reboot required, pending
                # updates, NTP/Satellite/SELinux, or simply missing data
                # for an unreachable host), plus that host's own latest
                # Disk & Filesystem check result - see overview_row_status()
                # and disk_check_overview_status(). Shown as a badge right
                # next to the hostname (so it's visible even before
                # scrolling) and as a whole-row tint (see .row-crit/
                # .row-warn in PAGE_CSS) so a problem host is obvious
                # without reading every cell.
                disk_status = disk_check_overview_status(hostname) if hostname else None
                idm_status = idm_healthcheck_overview_status(hostname) if hostname else None
                status_key, status_reasons, flagged_cols = overview_row_status(header, row, disk_status, idm_status)
                row_cls = ""
                if status_key == "CRITICAL":
                    row_cls = " class=\"row-crit\""
                elif status_key == "WARNING":
                    row_cls = " class=\"row-warn\""
                body.append("<tr" + row_cls + ">")
                for idx, cell in enumerate(row):
                    col_header = header[idx].strip().upper() if idx < len(header) else ""
                    norm_col = _norm_header(header[idx]) if idx < len(header) else ""
                    cell_classes = []
                    if col_header in NUM_COL_HEADERS:
                        cell_classes.append("num-col")
                    if norm_col in flagged_cols:
                        cell_classes.append("cell-alert")
                    cls = " class=\"" + " ".join(cell_classes) + "\"" if cell_classes else ""
                    if idx == 0 and hostname and is_valid_hostname(hostname):
                        link = "/host/" + quote(hostname)
                        if status_key == "PASS":
                            badge = render_status_badge("OK", "PASS")
                        else:
                            badge = render_status_badge(
                                "CRIT" if status_key == "CRITICAL" else "WARN",
                                status_key,
                                title="; ".join(status_reasons),
                            )
                        body.append(
                            "<td" + cls + ">" + badge + " <a href=\"" + link + "\">" + _e(cell) + "</a></td>"
                        )
                    else:
                        body.append("<td" + cls + ">" + _e(cell) + "</td>")
                # Pad any short rows (unreachable-host placeholders) so
                # columns still line up under the full header.
                for _ in range(len(header) - len(row)):
                    body.append("<td></td>")
                body.append("</tr>")
            body.append("</table></div>")

    extra_hosts = sorted(set(list_hosts_with_reports()) - {r[0].strip() for r in rows if r})
    if extra_hosts:
        body.append("<div class=\"card\"><h2>Other hosts with report history</h2>"
                     "<p class=\"muted\">These hosts have linuxdb_reports data but no current "
                     "hostinfo.csv row (host_info run may not have targeted them):</p><ul>")
        for h in extra_hosts:
            if is_valid_hostname(h):
                body.append("<li><a href=\"/host/" + quote(h) + "\">" + _e(h) + "</a></li>")
        body.append("</ul></div>")

    return PAGE_HEAD + "".join(body) + PAGE_TAIL


def render_package_search(params):
    """
    Fleet-wide "which servers have package X installed?" search -
    reads every host's latest rpm_report run (see
    search_installed_packages()) rather than any per-host page. Reuses
    the same magnifying-glass search-box markup as render_host_jump()
    (host-jump / host-jump-row / host-jump-label CSS classes), but this
    form is a plain text query (no datalist - there's no fixed list of
    package names to autocomplete against) posting to GET /packages
    rather than /goto-host.

    Results render as a sortable .table-scroll table - same sortable-
    <th> + SORT_ARROW_HTML markup, and the same generic TABLE_SORT_JS,
    as the Overview fleet table - so clicking HOSTNAME/PACKAGE/VERSION/
    etc. re-sorts client-side with no extra code needed here. Each
    HOSTNAME cell links to that host's own Installed Packages tab
    (?check=rpm_report) for the full per-host listing.
    """
    query = (params.get("q", [""])[0] or "").strip()
    body = ["<h2>Package Search</h2>"]
    body.append(
        "<p class=\"muted\">Search every host's latest installed-package "
        "inventory - e.g. search &ldquo;firefox&rdquo; to see every server "
        "that has it installed, with the exact version on each.</p>"
    )
    body.append("<form class=\"host-jump\" method=\"get\" action=\"/packages\" role=\"search\">")
    body.append("<label class=\"host-jump-label\" for=\"pkg-search-input\">Search installed packages</label>")
    body.append("<div class=\"host-jump-row\">")
    body.append(
        "<svg width=\"18\" height=\"18\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" "
        "stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\">"
        "<circle cx=\"11\" cy=\"11\" r=\"7\"></circle><line x1=\"21\" y1=\"21\" x2=\"16.65\" y2=\"16.65\"></line></svg>"
    )
    body.append(
        "<input type=\"text\" id=\"pkg-search-input\" name=\"q\" value=\"" + _e(query) + "\" "
        "placeholder=\"e.g. firefox\" autocomplete=\"off\" autocapitalize=\"off\" "
        "spellcheck=\"false\" required>"
    )
    body.append("<button type=\"submit\">Search</button>")
    body.append("</div>")
    body.append("</form>")

    if "rpm_report" not in VALID_CHECK_KEYS:
        body.append(
            "<div class=\"notice\">Installed-package data isn't configured on this "
            "deployment yet. Add an <code>rpm_report</code> entry to "
            "<code>linuxdb_reports_checks</code> and redeploy to enable this search.</div>"
        )
        return PACKAGES_PAGE_HEAD + "".join(body) + PAGE_TAIL

    if not query:
        body.append("<p class=\"muted\">Type a package name above and press Enter.</p>")
        return PACKAGES_PAGE_HEAD + "".join(body) + PAGE_TAIL

    results = search_installed_packages(query)
    if not results:
        body.append(
            "<div class=\"notice\">No installed package matching &ldquo;" + _e(query)
            + "&rdquo; was found on any host with collected data. Check the spelling, or "
            "collect fresh data with:<br><code>ansible-playbook playbooks/linuxdb_dashboard.yml "
            "--tags linuxdb_reports</code></div>"
        )
        return PACKAGES_PAGE_HEAD + "".join(body) + PAGE_TAIL

    body.append(
        "<p class=\"muted\">" + str(len(results)) + " match" + ("" if len(results) == 1 else "es")
        + " across " + str(len({r[0] for r in results})) + " host"
        + ("" if len({r[0] for r in results}) == 1 else "s") + " for &ldquo;" + _e(query) + "&rdquo;:</p>"
    )
    body.append("<div class=\"table-scroll report-output\"><table><tr>")
    columns = ["HOSTNAME", "PACKAGE", "VERSION", "RELEASE", "ARCH", "RUN"]
    for col_idx, h in enumerate(columns):
        body.append(
            "<th class=\"sortable\" data-col=\"" + str(col_idx) + "\">" + _e(h)
            + SORT_ARROW_HTML + "</th>"
        )
    body.append("</tr>")
    for hostname, package, version, release, arch, run_ts in results:
        body.append("<tr>")
        if is_valid_hostname(hostname):
            body.append(
                "<td><a href=\"/host/" + quote(hostname) + "?check=rpm_report\">"
                + _e(hostname) + "</a></td>"
            )
        else:
            body.append("<td>" + _e(hostname) + "</td>")
        for cell in (package, version, release, arch, run_ts):
            body.append("<td>" + _e(cell) + "</td>")
        body.append("</tr>")
    body.append("</table></div>")

    return PACKAGES_PAGE_HEAD + "".join(body) + PAGE_TAIL


def render_host_detail(hostname, params):
    header, rows, _summary, _unreachable, hostinfo_error = parse_hostinfo()
    hostinfo_row = find_hostinfo_row(header, rows, hostname)

    body = ["<p><a href=\"/\">&larr; All hosts</a></p>"]

    # Same "jump to a host" box as the Overview page, so you can pivot
    # straight to a *different* host's reports from here without backing
    # out to "All hosts" first and hunting for it in the fleet table. An
    # invalid hostname bounces back to this same page (return_to) rather
    # than to Overview - see /goto-host.
    known_hosts = sorted(set(r[0].strip() for r in rows if r and r[0].strip()) | set(list_hosts_with_reports()))
    search_error = (params.get("host_search_error", [""])[0] or "").strip()
    not_found_error = (params.get("host_not_found", [""])[0] or "").strip()
    body.append(render_host_jump(
        known_hosts, search_error,
        return_to="/host/" + quote(hostname),
        heading="Jump to another host&rsquo;s reports",
        not_found_error=not_found_error,
    ))

    body.append("<div class=\"card\"><h2>" + _e(hostname) + "</h2>")

    if hostinfo_row:
        # Same worst-finding logic that drives the Overview badge, reused
        # here so an individual offending value (e.g. CHRONYD: inactive)
        # is highlighted bold red on this per-host page too - not just
        # implied by a badge/tooltip someone might not notice.
        disk_status = disk_check_overview_status(hostname)
        idm_status = idm_healthcheck_overview_status(hostname)
        _status_key, _reasons, flagged_cols = overview_row_status(header, hostinfo_row, disk_status, idm_status)
        body.append("<table class=\"host-info-table\">")
        for h, v in zip(header, hostinfo_row):
            flagged = _norm_header(h) in flagged_cols
            row_cls = " class=\"row-alert\"" if flagged else ""
            cell_cls = " class=\"cell-alert\"" if flagged else ""
            body.append("<tr" + row_cls + "><th>" + _e(h) + "</th><td" + cell_cls + ">" + _e(v) + "</td></tr>")
        body.append("</table>")
    elif hostinfo_error:
        body.append("<div class=\"notice\">" + _e(hostinfo_error) + "</div>")
    else:
        body.append("<div class=\"notice\">No hostinfo.csv row for this host.</div>")
    body.append("</div>")

    # IdM Health is added to this host's tab bar only when it actually has
    # data (see host_has_idm_healthcheck()) - unlike the always-shown Config
    # Files tab, most hosts in a fleet aren't IdM servers and an always-empty
    # tab there would just be clutter.
    host_tabs = list(ALL_TABS)
    if host_has_idm_healthcheck(hostname):
        host_tabs.append((IDM_HEALTHCHECK_CHECK_KEY, "IdM Health"))

    requested_check = params.get("check", [""])[0]
    active_check = requested_check if requested_check in VALID_CHECK_KEYS else (
        CHECK_KEYS[0] if CHECK_KEYS else None
    )

    if active_check:
        body.append("<div class=\"tabs\">")
        for key, label in host_tabs:
            cls = "tab active" if key == active_check else "tab"
            link = "/host/" + quote(hostname) + "?check=" + quote(key)
            body.append("<a class=\"" + cls + "\" href=\"" + link + "\">" + _e(label) + "</a>")
        body.append("</div>")

        if active_check == CONFIG_FILES_CHECK_KEY:
            body.append("<div class=\"card\">")
            body.append(render_config_files_section(hostname, params))
            body.append("</div>")
            return PAGE_HEAD + "".join(body) + PAGE_TAIL

        if active_check == IDM_HEALTHCHECK_CHECK_KEY:
            body.append("<div class=\"card\">")
            body.append(render_idm_healthcheck_section(hostname, params))
            body.append("</div>")
            return PAGE_HEAD + "".join(body) + PAGE_TAIL

        timestamps = list_check_timestamps(hostname, active_check)[: config.MAX_HISTORY_LIST]
        requested_ts = params.get("ts", [""])[0]
        if requested_ts and is_valid_timestamp(requested_ts) and requested_ts in timestamps:
            active_ts = requested_ts
        else:
            active_ts = resolve_latest_timestamp(hostname, active_check)

        body.append("<div class=\"card\">")
        if not active_ts:
            body.append(
                "<div class=\"notice\">No " + _e(CHECK_LABELS.get(active_check, active_check))
                + " data yet for this host. Collect it with:<br><code>ansible-playbook "
                "playbooks/linuxdb_dashboard.yml --tags linuxdb_reports --limit "
                + _e(hostname) + "</code></div>"
            )
        else:
            if timestamps:
                body.append("<form class=\"history\" method=\"get\" action=\"/host/" + quote(hostname) + "\">")
                body.append("<input type=\"hidden\" name=\"check\" value=\"" + _e(active_check) + "\">")
                body.append("<label for=\"ts\">Run:</label>")
                body.append("<select name=\"ts\" id=\"ts\">")
                for ts in timestamps:
                    sel = " selected" if ts == active_ts else ""
                    body.append("<option value=\"" + _e(ts) + "\"" + sel + ">" + _e(ts) + "</option>")
                body.append("</select>")
                body.append("<button type=\"submit\">View</button>")
                body.append("</form>")

            content, rc, read_error = read_check_output(hostname, active_check, active_ts)
            if read_error:
                body.append("<div class=\"notice error\">" + _e(read_error) + "</div>")
            else:
                body.append(
                    "<p class=\"muted\">Run " + _e(active_ts) + " &nbsp;|&nbsp; exit code: "
                    "<span class=\"" + rc_class(rc) + "\">" + _e(rc if rc is not None else "unknown")
                    + "</span></p>"
                )
                body.append('<div class="report-output">' + render_check_body(active_check, content) + "</div>")
        body.append("</div>")

    return PAGE_HEAD + "".join(body) + PAGE_TAIL


def render_guide():
    """The team-facing Operational Support Guide, served as a static
    page at /guide. Content is pre-rendered at build time (see
    GUIDE_HTML above and docs/render_guide.py) - this function only
    wraps it in the same page chrome (header/nav/CSS) as every other
    page, it does no markdown conversion itself at request time.
    """
    guide_html = _load_guide_html()
    if not guide_html:
        return (
            GUIDE_PAGE_HEAD
            + "<h2>Operational Support Guide</h2>"
            + "<div class=\"notice error\">Guide content isn't available on "
              "this host yet - the file that's supposed to be deployed "
              "alongside this app (guide_content.html) is missing. This "
              "clears itself up on the next full deploy of this "
              "dashboard.</div>"
            + PAGE_TAIL
        )
    return GUIDE_PAGE_HEAD + '<div class="card guide-content">' + guide_html + "</div>" + PAGE_TAIL


def render_notfound():
    return PAGE_HEAD + "<div class=\"notice error\">Page not found.</div>" + PAGE_TAIL


###############################################################################
# Authentication
###############################################################################
# HTTP Basic Auth, checked directly here rather than delegated to a reverse
# proxy - see the "Authentication" section of README.md. Stateless: no
# sessions, no cookies, credentials are re-sent (and re-checked) with every
# request. Deliberately still stdlib-only (base64 + hmac), same philosophy
# as the rest of this app.


def _unauthorized_response(start_response):
    body = b"401 Unauthorized\n"
    start_response(
        "401 Unauthorized",
        [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("WWW-Authenticate", 'Basic realm="linuxdb-dashboard", charset="UTF-8"'),
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


###############################################################################
# WSGI entry point
###############################################################################

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

    path = environ.get("PATH_INFO", "/") or "/"
    try:
        qs = parse_qs(environ.get("QUERY_STRING", ""))
        content_type = "text/html; charset=utf-8"
        extra_headers = []

        if path == "/":
            body = render_home(qs)
            status = "200 OK"
        elif path == "/goto-host":
            # Backs the Overview page's "Jump to a host's reports" search
            # box - a real GET redirect (not just client-side JS) so it
            # works with JavaScript disabled too. Same is_valid_hostname()
            # charset check as the /host/ route itself, PLUS a host_exists()
            # check - a made-up/typo'd hostname that isn't in hostinfo.csv
            # or report history bounces back with a "not found" notice
            # instead of opening /host/<name>'s empty page, which otherwise
            # looks like a real (just not-yet-collected) host.
            typed = (qs.get("hostname", [""])[0] or "").strip()
            # Where to bounce back to on an *invalid*/unknown hostname -
            # defaults to the Overview page, but a per-host report page's
            # own jump box stamps its own path on as return_to so the error
            # re-appears there instead of yanking the user back to
            # Overview. Only ever a same-site path (starts with a single
            # "/", never "//" or a scheme) - never trust this into an open
            # redirect.
            return_to = (qs.get("return_to", [""])[0] or "").strip()
            if not return_to.startswith("/") or return_to.startswith("//") or ":" in return_to:
                return_to = "/"
            if typed and is_valid_hostname(typed):
                if host_exists(typed):
                    start_response("302 Found", [("Location", "/host/" + quote(typed))])
                    return [b""]
                sep = "&" if "?" in return_to else "?"
                location = return_to + sep + "host_not_found=" + quote(typed)
            elif typed:
                sep = "&" if "?" in return_to else "?"
                location = return_to + sep + "host_search_error=" + quote(typed)
            else:
                location = return_to
            start_response("302 Found", [("Location", location)])
            return [b""]
        elif path == "/export.csv":
            body = render_overview_csv(qs)
            status = "200 OK"
            content_type = "text/csv; charset=utf-8"
            extra_headers = [
                ("Content-Disposition", "attachment; filename=\"" + _csv_export_filename(qs) + "\""),
            ]
        elif path == "/guide":
            body = render_guide()
            status = "200 OK"
        elif path == "/packages":
            body = render_package_search(qs)
            status = "200 OK"
        elif path.startswith("/host/"):
            raw_hostname = unquote(path[len("/host/"):].split("/")[0])
            if not is_valid_hostname(raw_hostname):
                body = render_notfound()
                status = "404 Not Found"
            else:
                body = render_host_detail(raw_hostname, qs)
                status = "200 OK"
        else:
            body = render_notfound()
            status = "404 Not Found"

        encoded = body.encode("utf-8")
        start_response(status, [
            ("Content-Type", content_type),
            ("Content-Length", str(len(encoded))),
        ] + extra_headers)
        return [encoded]

    except Exception:  # noqa: BLE001 - surface a friendly message, log detail server-side
        # This app is deployed as a standalone stdlib WSGI service (see
        # roles/linuxdb_dashboard) - systemd captures stderr directly, so
        # `journalctl -u linuxdb-dashboard` shows the full traceback.
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        error_body = (
            PAGE_HEAD
            + "<div class=\"notice error\">Something went wrong - check "
              "'journalctl -u linuxdb-dashboard' for details.</div>"
            + PAGE_TAIL
        ).encode("utf-8")
        start_response("500 Internal Server Error", [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(error_body))),
        ])
        return [error_body]
