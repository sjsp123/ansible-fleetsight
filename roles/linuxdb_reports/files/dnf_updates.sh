#!/usr/bin/env bash
#
# dnf_updates.sh
#
# Purpose:
#   Read-only pending-package-update status. Lists every package with
#   an update available (from whatever repos/metadata this host already
#   has configured — Red Hat Satellite content views for this fleet),
#   flags which of those are security errata, and reports whether a
#   reboot is currently outstanding from an already-applied update.
#
#   This script NEVER installs, upgrades, or removes anything — it only
#   queries. Actually applying updates stays the job of
#   roles/system_update (playbooks/update_system.yml), which is a
#   separate, deliberate, change-making play. This check is safe to run
#   on any schedule (it's the same read-only pattern as df_check.sh /
#   proc_check.sh / svr_check.sh / system_stats.sh).
#
# Metadata freshness:
#   Uses whatever repo metadata cache already exists on the host at run
#   time — this is a status *snapshot*, not a forced repo sync. If your
#   metadata only refreshes on its own cache-expiry schedule (or via a
#   separate `dnf makecache` cron), the pending-update list here is only
#   as fresh as that. Set FORCE_REFRESH=true below if this host's
#   collection cadence is sparse enough that you'd rather pay the
#   (network-dependent) cost of a metadata refresh on every run.
#
# Output Format:
#   HOSTNAME|PACKAGE|STATUS|VALUE - a SINGLE contiguous pipe-delimited
#   block (package rows, then a REBOOT_REQUIRED row, then a SUMMARY
#   row), deliberately with NO blank lines in between. app.py's table
#   parser (parse_pipe_block/render_check_body) stops at the first
#   blank line, so keeping every meaningful row unbroken is what makes
#   all of it show up in the dashboard tab as one status table - only
#   the trailing OVERALL_STATUS line (after the one blank line at the
#   very end) is deliberately outside that block: it's redundant with
#   SUMMARY's own STATUS cell, kept only for a human reading raw output
#   from an ad hoc terminal run.
#
# Status Values:
#   PASS      - no pending update / no reboot outstanding
#   INFO      - pending update, not flagged as security; or the
#               relevant tool isn't installed / status can't be
#               determined
#   WARNING   - pending update flagged as security errata; or a reboot
#               is outstanding
#   (CRITICAL is intentionally not used per-package — see SUMMARY row
#   below for how CRITICAL is decided at the overall level)
#
# Supported:
#   RHEL 8 / RHEL 9 / RHEL 10 (dnf). Falls back to yum's equivalent
#   subcommands where present (RHEL 7 / older EL) - untested against
#   this fleet, which is dnf-only, but kept as a graceful degrade
#   rather than hard-failing on an unexpected host.
#

set -uo pipefail

HOST=$(hostname -s)

# Set true to force a metadata refresh (dnf/yum --refresh) before
# checking - costs time and needs repo/network reachability every run.
# Default false: read whatever metadata cache already exists, same
# philosophy as the rest of this role's checks (fast, safe, repeatable).
FORCE_REFRESH=false

echo "HOSTNAME|PACKAGE|STATUS|VALUE"

PKG_MGR=""
if command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
elif command -v yum >/dev/null 2>&1; then
    PKG_MGR="yum"
fi

if [[ -z "$PKG_MGR" ]]; then
    echo "${HOST}|PACKAGE_MANAGER|INFO|Neither dnf nor yum found on this host"
    echo "SUMMARY|INFO|Cannot check for updates on this host"
    echo
    echo "OVERALL_STATUS|INFO"
    exit 0
fi

REFRESH_FLAG=""
if [[ "$FORCE_REFRESH" == "true" ]]; then
    REFRESH_FLAG="--refresh"
fi

##############################################################################
# PENDING UPDATES
##############################################################################

UPDATE_RAW=$("$PKG_MGR" -q $REFRESH_FLAG check-update 2>/dev/null)
UPDATE_RC=$?

# check-update exit codes (both dnf and yum): 0 = no updates, 100 = updates
# available, anything else = the query itself failed (repo unreachable,
# no enabled repos, etc.) - NOT the same thing as "no updates pending", so
# it gets its own row rather than silently reporting a false PASS.
if [[ "$UPDATE_RC" != "0" && "$UPDATE_RC" != "100" ]]; then
    FIRST_LINE=$(echo "$UPDATE_RAW" | head -n1)
    echo "${HOST}|CHECK-UPDATE|WARNING|${PKG_MGR} check-update failed (exit ${UPDATE_RC}): ${FIRST_LINE:-see logs}"
    echo "SUMMARY|WARNING|Unable to determine pending-update status"
    echo
    echo "OVERALL_STATUS|WARNING"
    exit 1
fi

# Package lines look like "name.arch   version-release   repo". Filters
# out the "Last metadata expiration check: ..." banner line (no dot in
# field 1), blank lines, the "Obsoleting Packages" section header, and
# that section's indented "@System" continuation lines (the package
# being obsoleted, not the new one) - best-effort, matches this role's
# existing pragmatic parsing style (see df_check.sh's own comments).
PENDING=$(echo "$UPDATE_RAW" | awk '
    $1 ~ /^[^[:space:]]+\.[^[:space:].]+$/ && NF >= 3 && $3 != "@System" {
        print
    }
')

if [[ -z "$PENDING" ]]; then
    echo "${HOST}|PACKAGES|PASS|No pending updates"
    PENDING_CNT=0
    SECURITY_CNT=0
else
    # Best-effort security-errata cross-reference: build the set of
    # NVRA strings (name-version-release.arch) that updateinfo reports
    # as security-relevant, then match each pending package against it.
    # Not available on every fleet (needs updateinfo/security metadata
    # published by the repo, e.g. Satellite errata sync) - if the
    # command fails or returns nothing, every pending package is simply
    # reported INFO rather than WARNING; nothing here treats "unknown"
    # as "definitely not security".
    SECURITY_SET=$("$PKG_MGR" -q updateinfo list updates --security 2>/dev/null | awk 'NF>=3{print $NF}')

    SECURITY_CNT=0
    PENDING_CNT=0
    while IFS= read -r LINE; do
        [[ -z "$LINE" ]] && continue
        PENDING_CNT=$((PENDING_CNT + 1))

        NAME_ARCH=$(echo "$LINE" | awk '{print $1}')
        VER_REL=$(echo "$LINE" | awk '{print $2}')
        REPO=$(echo "$LINE" | awk '{print $3}')
        PKG_NAME="${NAME_ARCH%.*}"
        PKG_ARCH="${NAME_ARCH##*.}"
        NVRA="${PKG_NAME}-${VER_REL}.${PKG_ARCH}"

        STATUS="INFO"
        if [[ -n "$SECURITY_SET" ]] && grep -qxF "$NVRA" <<< "$SECURITY_SET"; then
            STATUS="WARNING"
            SECURITY_CNT=$((SECURITY_CNT + 1))
        fi

        echo "${HOST}|${NAME_ARCH}|${STATUS}|${VER_REL} available (${REPO})"
    done <<< "$PENDING"
fi

##############################################################################
# REBOOT REQUIRED
#
# Reflects an update ALREADY applied earlier (e.g. a kernel) that this
# host hasn't rebooted into yet - distinct from the pending-updates list
# above, which is updates not yet applied at all. Needs dnf-utils/
# yum-utils' needs-restarting; degrades to INFO (not WARNING) if that
# tool isn't installed, matching svr_check.sh's own graceful-skip
# pattern for optional tooling.
##############################################################################

if command -v needs-restarting >/dev/null 2>&1; then
    needs-restarting -r >/dev/null 2>&1
    NEEDS_REBOOT_RC=$?
    if [[ "$NEEDS_REBOOT_RC" != "0" ]]; then
        echo "${HOST}|REBOOT_REQUIRED|WARNING|Yes - an already-applied update needs a reboot to take effect"
        REBOOT_REQUIRED=1
    else
        echo "${HOST}|REBOOT_REQUIRED|PASS|No"
        REBOOT_REQUIRED=0
    fi
else
    echo "${HOST}|REBOOT_REQUIRED|INFO|needs-restarting not installed (dnf-utils/yum-utils) - unknown"
    REBOOT_REQUIRED=0
fi

##############################################################################
# SUMMARY / OVERALL STATUS
#
# Worst of: any security-flagged pending update, or a reboot already
# outstanding -> CRITICAL; any pending update at all (non-security) ->
# WARNING; otherwise PASS. Same three-tier PASS/WARNING/CRITICAL exit
# convention as df_check.sh (0/1/2) so the dashboard's per-check exit
# code colouring is consistent across tabs.
##############################################################################

if [[ "$SECURITY_CNT" -gt 0 ]]; then
    echo "SUMMARY|CRITICAL|${PENDING_CNT} pending update(s), ${SECURITY_CNT} security"
    echo
    echo "OVERALL_STATUS|CRITICAL"
    exit 2
elif [[ "$REBOOT_REQUIRED" -eq 1 ]]; then
    echo "SUMMARY|CRITICAL|${PENDING_CNT} pending update(s); reboot already outstanding"
    echo
    echo "OVERALL_STATUS|CRITICAL"
    exit 2
elif [[ "$PENDING_CNT" -gt 0 ]]; then
    echo "SUMMARY|WARNING|${PENDING_CNT} pending update(s), none flagged security"
    echo
    echo "OVERALL_STATUS|WARNING"
    exit 1
else
    echo "SUMMARY|PASS|No pending updates, no reboot outstanding"
    echo
    echo "OVERALL_STATUS|PASS"
    exit 0
fi
