#!/usr/bin/env bash
#
# df_check.sh
#
# Purpose:
#   Filesystem utilisation and mount validation.
#
# Supported:
#   RHEL 8 / RHEL 9 / RHEL 10
#
# Output:
#   Pipe-delimited for easy parsing by:
#     - Ansible
#     - Splunk
#     - ELK
#     - Monitoring scripts
#

set -euo pipefail

HOST=$(hostname -s)

# Utilisation thresholds (percent used). Two tiers, extending this
# script's own existing PASS(0)/WARNING(1) exit code to a third
# CRITICAL(2) - app.py's rc_class() (and the Overview page's traffic
# light, wired up separately) already treats any non-0/1 code as the
# worst (red) bucket, so this needs no dashboard-side exit-code change:
#   < WARN_THRESHOLD            -> PASS
#   >= WARN_THRESHOLD, < CRIT   -> WARNING (headroom is getting tight,
#                                  worth a look but rarely urgent - log
#                                  rotation/cleanup can usually recover it)
#   >= CRIT_THRESHOLD           -> CRITICAL (genuinely close to full -
#                                  a busy /var/log or package cache can
#                                  eat the remaining few percent fast)
# Tune these two numbers here if they don't fit your fleet - they're
# plain shell variables, nothing else in this script needs to change.
WARN_THRESHOLD=85
CRIT_THRESHOLD=95

# Pseudo/virtual filesystem types excluded from the WARNING/CRITICAL
# evaluation below (they still show up in the full utilisation table
# above, just not flagged) - tmpfs/devtmpfs are RAM-backed and sizing
# is by design, squashfs (snap/live images) and overlay are normally
# reported ~100% full on purpose, iso9660/efivarfs are fixed-size and
# not a capacity concern. Flagging these would just be false-positive
# noise on the dashboard's traffic light for something that isn't
# actually a disk-space problem.
EXCLUDED_TYPES='^(tmpfs|devtmpfs|squashfs|overlay|iso9660|efivarfs)$'

FSTAB_FILE=$(mktemp)
MOUNT_FILE=$(mktemp)

cleanup() {
    rm -f "$FSTAB_FILE" "$MOUNT_FILE"
}
trap cleanup EXIT

##############################################################################
# FILESYSTEM UTILISATION
##############################################################################

echo "HOSTNAME|FILESYSTEM|TYPE|SIZE|USED|USE%|MOUNTPOINT"

# Every filesystem df knows about - deliberately no -x type exclusions
# and no type filter in the awk, so tmpfs/devtmpfs/squashfs/efivarfs/
# iso9660/etc all show up too rather than being silently dropped.
df -hPT \
| awk -v host="$HOST" '
NR > 1 {
    printf "%s|%s|%s|%s|%s|%s|%s\n",
        host,
        $1,
        $2,
        $3,
        $4,
        $6,
        $7
}
'

echo

##############################################################################
# HIGH UTILISATION CHECK
##############################################################################

echo "FILESYSTEM UTILISATION WARNINGS"
echo "--------------------------------"

WARNINGS=$(df -PTh \
| awk -v warn="$WARN_THRESHOLD" -v crit="$CRIT_THRESHOLD" -v excl="$EXCLUDED_TYPES" '
NR > 1 && $2 !~ excl {
    pct=$6
    gsub("%","",pct)
    # Force pct to a genuine numeric value before comparing. gsub()
    # strips the numeric-string ("strnum") typing awk would otherwise
    # give a field like $6 - once mutated by gsub, "9" is just a plain
    # string, and comparing it against warn/crit (numeric-looking -v
    # values) silently falls back to a LEXICOGRAPHIC string compare.
    # That let a clean 9% filesystem trip the WARNING branch, because
    # as strings "9" >= "85" is true (leading "9" > leading "8").
    # The +0 below forces a true numeric type so >= is always a real
    # numeric comparison, matching the pattern already used correctly
    # in svr_check.sh check_inodes().
    pct = pct + 0

    if (pct >= crit) {
        printf "CRITICAL|%s|%s%%|%s\n",
            $7,
            pct,
            $1
    } else if (pct >= warn) {
        printf "WARNING|%s|%s%%|%s\n",
            $7,
            pct,
            $1
    }
}')

if [[ -n "$WARNINGS" ]]; then
    echo "$WARNINGS"
else
    echo "NONE"
fi

echo

# Counts feed OVERALL_STATUS below - guarded so an all-clear result
# (grep finds nothing, exits 1) never trips 'set -e' and aborts the
# script.
DISK_CRIT_CNT=0
DISK_WARN_CNT=0
if [[ -n "$WARNINGS" ]]; then
    DISK_CRIT_CNT=$(printf '%s\n' "$WARNINGS" | grep -c '^CRITICAL|' || true)
    DISK_WARN_CNT=$(printf '%s\n' "$WARNINGS" | grep -c '^WARNING|' || true)
fi

##############################################################################
# BUILD FSTAB DATASET
##############################################################################

awk '
BEGIN { OFS="|" }

# Skip comments and blank lines
/^#/ || NF == 0 {
    next
}

# Skip pseudo filesystems
$3 ~ /^(proc|sysfs|devpts|tmpfs|swap)$/ {
    next
}

{
    print $2, $3
}
' /etc/fstab | sort -u > "$FSTAB_FILE"

##############################################################################
# BUILD CURRENT MOUNT DATASET
#
# This reconciliation section intentionally stays scoped to "real"
# (non-pseudo) filesystem types on both sides, unlike the display and
# warnings sections above which now show everything df reports. Pseudo
# mounts like tmpfs/proc/devpts rarely have a clean 1:1 fstab entry to
# compare against, so including them here would just produce noisy,
# meaningless MOUNT/FSTAB mismatches rather than real ones.
#
# Autofs-triggered submounts are excluded too, for the same reason: an
# indirect-map path (e.g. a user's NFS home directory under /home,
# mounted on demand only while they're logged in) has no /etc/fstab
# entry by design - it's managed by autofs maps (/etc/auto.master,
# /etc/auto.home, etc.) instead, not fstab. Flagging its absence from
# fstab would be a false positive exactly like the pseudo-filesystem
# exclusions above, just for this reconciliation check instead of the
# utilisation one. Detected generically (no per-path hardcoding) by
# gathering every currently-active autofs base mountpoint first, then
# skipping any real-filesystem mount found underneath one.
##############################################################################

AUTOFS_BASES=$(findmnt -rn -t autofs -o TARGET 2>/dev/null || true)

findmnt -rn -o TARGET,FSTYPE |
awk -v bases="$AUTOFS_BASES" '
BEGIN {
    OFS="|"
    n = split(bases, base_arr, "\n")
}

$2 ~ /^(xfs|ext4|ext3|ext2|vfat|nfs|nfs4)$/ {
    under_autofs = 0
    for (i = 1; i <= n; i++) {
        if (base_arr[i] != "" && index($1, base_arr[i] "/") == 1) {
            under_autofs = 1
            break
        }
    }
    if (!under_autofs) print $1, $2
}
' | sort -u > "$MOUNT_FILE"

##############################################################################
# FSTAB VALIDATION
##############################################################################

echo "MOUNT/FSTAB VALIDATION"
echo "----------------------"

if diff -q "$MOUNT_FILE" "$FSTAB_FILE" >/dev/null 2>&1
then
    echo "STATUS|PASS|Mounted filesystems match fstab"

    MNTCNT=0
    FSTCNT=0
else
    echo "STATUS|WARNING|Differences detected"
    echo

    diff "$MOUNT_FILE" "$FSTAB_FILE" | grep -E '^<|^>' || true

    MNTCNT=$(comm -23 "$MOUNT_FILE" "$FSTAB_FILE" | wc -l)
    FSTCNT=$(comm -13 "$MOUNT_FILE" "$FSTAB_FILE" | wc -l)
fi

echo
echo "SUMMARY"
echo "-------"
echo "< | MOUNTED ${MNTCNT} ; FSTAB 0"
echo "> | MOUNTED 0 ; FSTAB ${FSTCNT}"
echo

##############################################################################
# OVERALL STATUS
#
# Worst of two independent findings: filesystem utilisation (above) and
# mounted-vs-fstab reconciliation (just above this block). CRITICAL
# utilisation always wins outright; otherwise it's WARNING if either
# utilisation is elevated or a mount/fstab mismatch was found, else PASS.
##############################################################################

if [[ "$DISK_CRIT_CNT" -gt 0 ]]
then
    echo "OVERALL_STATUS|CRITICAL"
    exit 2
elif [[ "$DISK_WARN_CNT" -gt 0 || "$MNTCNT" -ne 0 || "$FSTCNT" -ne 0 ]]
then
    echo "OVERALL_STATUS|WARNING"
    exit 1
else
    echo "OVERALL_STATUS|PASS"
    exit 0
fi
