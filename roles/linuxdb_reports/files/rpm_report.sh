#!/usr/bin/env bash
#
# rpm_report.sh
#
# Purpose:
#   Read-only inventory of every RPM package currently installed on this
#   host — name, version, release, architecture and install date. This
#   is the data behind two things in the dashboard:
#     - This host's own "Installed Packages" tab (just this host's list).
#     - The fleet-wide Package Search page (`/packages` in app.py), which
#       reads every host's latest rpm_report run and answers "which
#       servers have package X installed?" without touching any host
#       directly — e.g. searching "firefox" returns every host that has
#       it, with the exact version on each.
#
#   Nothing here installs, upgrades, or removes anything — it only reads
#   the existing RPM database (`rpm -qa`), same read-only philosophy as
#   every other check in this role.
#
# Output Format:
#   HOSTNAME|PACKAGE|VERSION|RELEASE|ARCH|INSTALLED - one row per
#   installed package, sorted alphabetically by package name. Unlike
#   df_check.sh/dnf_updates.sh/svr_check.sh, there is deliberately no
#   STATUS column and no WARNING/CRITICAL tier here — this is a plain
#   inventory listing, not a threshold-based check (there's no "too
#   many packages installed" condition to flag), so it always exits 0.
#
# Supported:
#   Any RPM-based host (RHEL, Oracle Linux, CentOS, Rocky, Fedora) with
#   the `rpm` command available. No `dnf`/`yum` dependency at all, so
#   this still works even on a host whose package manager itself is
#   unreachable or misconfigured — `rpm -qa` only reads the local RPM
#   database, it never touches a repo.
#

set -uo pipefail

HOST=$(hostname -s)

echo "HOSTNAME|PACKAGE|VERSION|RELEASE|ARCH|INSTALLED"

if ! command -v rpm >/dev/null 2>&1; then
    echo "${HOST}|RPM|N/A|N/A|N/A|rpm command not found on this host"
    exit 0
fi

# %{INSTALLTIME:date} renders as e.g. "Tue 12 Aug 2025 09:14:02 AM BST" —
# contains spaces but never a literal "|", so it's still safe for the
# pipe-delimited format app.py's parse_pipe_block() expects. Sorted by
# package name (not install date) so the per-host tab and the fleet
# search results both read in a predictable, alphabetical order.
rpm -qa --queryformat '%{NAME}|%{VERSION}|%{RELEASE}|%{ARCH}|%{INSTALLTIME:date}\n' 2>/dev/null \
    | sort -t'|' -k1,1f \
    | awk -F'|' -v host="$HOST" '{print host "|" $0}'

exit 0
