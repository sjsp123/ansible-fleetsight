#!/usr/bin/env bash
#
# host_health_check.sh
#
# Purpose:
#   Live/dynamic Linux operational health signals - things that change
#   run-to-run rather than static host facts. OS/kernel/arch, uptime,
#   filesystem %, NTP sync, SELinux mode, reboot-required and
#   MFR/MODEL/SERIAL are intentionally NOT covered here: those already
#   appear on the fleet Overview table (roles/hostinfo) or the Disk &
#   Filesystem tab, so repeating them here would just show the same
#   handful of facts on every tab. Likewise, per-unit failed-service
#   detection lives on Overview (FAILED_SVC_CNT/FAILED_SVCS, roles/
#   hostinfo) - not duplicated here either.
#
# Sections:
#   - Resource pressure: load (vs core count), memory (MemAvailable-
#     based, not naive "used"), swap % and active paging, CPU
#     utilisation/iowait/steal, inode usage
#   - Disk performance & storage health: per-device iostat latency/
#     utilisation, unexpectedly read-only filesystems, LVM thin-pool
#     usage, software RAID (mdadm) degradation
#   - Logs: recent OOM-killer events (incl. cgroup OOM), recent kernel/
#     hardware errors, leftover crash dumps from a prior kernel panic
#   - Process/network: D-state (hung I/O) processes, NIC errors/
#     drops/overruns, carrier/link state, TCP/socket exhaustion
#   - Services: restart-count "flapping" on the small set of daemons
#     this fleet already tracks (sshd/firewalld/chronyd/auditd/crond/
#     sssd) - NOT general failed-unit detection (that's Overview's job)
#   - Auth & identity: failed SSH/sudo logins, locked local accounts,
#     SSSD status, IdM/LDAP domain reachability, Kerberos KDC
#     reachability, DNS self-resolution, certmonger certificate
#     expiry, chrony clock-offset magnitude, kdump crash-capture status
#
# Output Format:
#   HOSTNAME|CHECK|STATUS|VALUE
#
# Status Values:
#   INFO      - informational, or the relevant tool isn't installed
#   PASS
#   WARNING
#   CRITICAL
#
# Every external command this script depends on (journalctl, systemctl,
# vmstat, iostat/sysstat, ausearch/audit, ss/iproute, lvm2, mdadm, ip,
# firewalld/auditd/chronyd/crond, krb5-workstation, sssd-tools,
# certmonger) ships in RHEL's own BaseOS/AppStream repos - nothing here
# requires EPEL. None of these are installed BY this script; if a tool
# isn't present (or isn't in use, e.g. no LVM thin pools or no software
# RAID on a given host) that check reports INFO and moves on rather
# than failing.
#
# Deliberately out of scope (see role README for the full rationale):
#   - Multipath status - explicitly dropped previously as low-signal
#     for this fleet (no SAN-attached hosts); revisit if that changes.
#   - rpm -Va package verification - too slow/heavy for a 6-hourly
#     report; better suited to an occasional standalone audit.
#   - Slab growth / trend-based metrics - need state persisted across
#     runs; this script is stateless and point-in-time only.
#   - Application-level checks (HTTP endpoints, DB connections, etc.) -
#     this is a host/OS health check, not an app-specific monitor.
#
# Supported:
#   RHEL 7 / RHEL 8 / RHEL 9 / RHEL 10
#

set -uo pipefail

export LC_ALL=C

HOST=$(hostname -s)

MEM_WARN_THRESHOLD=80
MEM_CRIT_THRESHOLD=90
SWAP_WARN_THRESHOLD=50
LOAD_WARN_RATIO=1.0
LOAD_CRIT_RATIO=2.0
CPU_UTIL_WARN_THRESHOLD=85
CPU_UTIL_CRIT_THRESHOLD=95
CPU_STEAL_WARN_THRESHOLD=5
CPU_STEAL_CRIT_THRESHOLD=10
IOWAIT_WARN_THRESHOLD=20
IOWAIT_CRIT_THRESHOLD=40
INODE_WARN_THRESHOLD=85
DISK_UTIL_WARN_THRESHOLD=80
DISK_UTIL_CRIT_THRESHOLD=95
LVM_THINPOOL_WARN_THRESHOLD=80
LVM_THINPOOL_CRIT_THRESHOLD=90
SSH_FAILED_WARN_THRESHOLD=20
CERT_EXPIRY_WARN_DAYS=30
NET_ERR_WARN_THRESHOLD=10
SOCKET_WARN_THRESHOLD=5000
SERVICE_RESTART_WARN_THRESHOLD=3
CHRONY_OFFSET_WARN_MS=1000
CHRONY_OFFSET_CRIT_MS=5000

# Real (non-pseudo, non-network) filesystem types evaluated by the
# read-only mount check below. Add mountpoints here (space-separated,
# matched literally) that your own hardening baseline deliberately
# mounts read-only (e.g. a CIS profile that mounts /boot ro except
# during kernel updates) so they don't fire a false CRITICAL.
RO_EXCLUDE_MOUNTS=""

#############################################################################
# Generic Output Function
#############################################################################

report() {

    local CHECK="$1"
    local STATUS="$2"
    local VALUE="$3"

    printf "%s|%s|%s|%s\n" \
        "$HOST" \
        "$CHECK" \
        "$STATUS" \
        "$VALUE"
}

#############################################################################
# Load Average (vs. core count)
#
# A raw load average is meaningless without knowing how many cores are
# available to run it - a load of 4 is idle on a 32-core box and badly
# oversubscribed on a 2-core VM. Normalises 1-minute load against
# nproc; the previous version of this check reported the raw numbers
# only, with no PASS/WARNING/CRITICAL evaluation at all.
#############################################################################

check_load() {

    local LOAD1 LOAD5 LOAD15 CORES RATIO STATUS

    LOAD1=$(awk '{print $1}' /proc/loadavg)
    LOAD5=$(awk '{print $2}' /proc/loadavg)
    LOAD15=$(awk '{print $3}' /proc/loadavg)

    CORES=$(nproc 2>/dev/null)

    if [[ -z "$CORES" || "$CORES" -eq 0 ]]
    then
        report "LOAD_AVG" "INFO" "${LOAD1},${LOAD5},${LOAD15} (core count unavailable)"
        return 0
    fi

    RATIO=$(awk -v l="$LOAD1" -v c="$CORES" 'BEGIN { printf "%.2f", l/c }')

    STATUS="PASS"
    if awk -v r="$RATIO" -v t="$LOAD_CRIT_RATIO" 'BEGIN{exit !(r>=t)}'
    then
        STATUS="CRITICAL"
    elif awk -v r="$RATIO" -v t="$LOAD_WARN_RATIO" 'BEGIN{exit !(r>=t)}'
    then
        STATUS="WARNING"
    fi

    report "LOAD_AVG" "$STATUS" "${LOAD1},${LOAD5},${LOAD15} (${CORES} cores, 1m/core=${RATIO})"
}

#############################################################################
# Memory Usage
#
# Deliberately based on /proc/meminfo's kernel-computed MemAvailable,
# not a naive "used = total - free" or free(1)'s "used" column. Linux
# uses spare RAM for page cache/buffers that it will happily reclaim
# under pressure - counting that as "used" produces false alarms on a
# perfectly healthy box. MemAvailable is the kernel's own estimate of
# what could actually be given to a new process right now, which is
# the correct basis for a used-memory percentage.
#############################################################################

check_memory() {

    local TOTAL AVAILABLE PCT STATUS

    if [[ ! -r /proc/meminfo ]]
    then
        report "MEMORY" "INFO" "/proc/meminfo not readable"
        return 0
    fi

    TOTAL=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
    AVAILABLE=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)

    if [[ -z "$TOTAL" || -z "$AVAILABLE" || "$TOTAL" -eq 0 ]]
    then
        report "MEMORY" "INFO" "Could not read MemTotal/MemAvailable"
        return 0
    fi

    PCT=$(awk -v t="$TOTAL" -v a="$AVAILABLE" 'BEGIN { printf "%d", 100 - (a/t)*100 }')

    STATUS="PASS"
    if [[ "$PCT" -ge "$MEM_CRIT_THRESHOLD" ]]
    then
        STATUS="CRITICAL"
    elif [[ "$PCT" -ge "$MEM_WARN_THRESHOLD" ]]
    then
        STATUS="WARNING"
    fi

    report "MEMORY" "$STATUS" "${PCT}% Used (MemAvailable-based)"
}

#############################################################################
# Swap Usage
#
# Overview's SWAP_GB column is static capacity; this is how much of it
# is actually in use right now - active swapping is a real performance
# signal that capacity alone can't show.
#############################################################################

check_swap() {

    free -m | awk \
    -v host="$HOST" \
    -v warn="$SWAP_WARN_THRESHOLD" '

    /^Swap:/ {

        total=$2
        used=$3

        if (total == 0) {
            printf "%s|SWAP_USAGE|INFO|No swap configured\n", host
        } else {
            pct=int((used/total)*100)
            status="PASS"
            if (pct >= warn)
                status="WARNING"
            printf "%s|SWAP_USAGE|%s|%s%% Used\n", host, status, pct
        }
    }'
}

#############################################################################
# Swap Activity (pages in/out)
#
# % used (above) is capacity, not activity - a host can sit at 40%
# swap used but completely idle, or at 5% used but actively thrashing
# pages in and out right now. Reuses vmstat's 2-sample technique (see
# check_cpu_stats below): the first line is the since-boot average and
# is discarded, the second is the real-time sample. A single 1-second
# sample can miss brief bursts between report runs, but any nonzero
# si/so caught in the sample means paging was actively happening at
# that instant, which is worth flagging on a fleet sized for this
# workload.
#############################################################################

check_swap_activity() {

    if ! command -v vmstat >/dev/null 2>&1
    then
        report "SWAP_ACTIVITY" "INFO" "vmstat not installed"
        return 0
    fi

    local RAW SI_COL SO_COL SI SO

    RAW=$(vmstat 1 2 2>/dev/null)
    SI_COL=$(awk 'NR==2 { for (i=1;i<=NF;i++) if ($i=="si") print i }' <<< "$RAW")
    SO_COL=$(awk 'NR==2 { for (i=1;i<=NF;i++) if ($i=="so") print i }' <<< "$RAW")

    if [[ -z "$SI_COL" || -z "$SO_COL" ]]
    then
        report "SWAP_ACTIVITY" "INFO" "Could not locate si/so columns in vmstat output"
        return 0
    fi

    SI=$(awk -v col="$SI_COL" 'NR==4 {print $col}' <<< "$RAW")
    SO=$(awk -v col="$SO_COL" 'NR==4 {print $col}' <<< "$RAW")

    if [[ -z "$SI" || -z "$SO" ]]
    then
        report "SWAP_ACTIVITY" "INFO" "N/A"
        return 0
    fi

    if [[ "$SI" -eq 0 && "$SO" -eq 0 ]]
    then
        report "SWAP_ACTIVITY" "PASS" "si=0 so=0 (KB/s)"
    else
        report "SWAP_ACTIVITY" "WARNING" "si=${SI} so=${SO} (KB/s)"
    fi
}

#############################################################################
# CPU Stats: I/O Wait, Overall Utilisation, Steal Time
#
# All three come from the same single vmstat sample rather than
# running vmstat three separate times. Column positions vary across
# kernel/vmstat versions (some add a 'gu' guest column, bare-metal
# hosts have no 'st' column at all), so the header row is parsed to
# find each one rather than assuming fixed field numbers - 'st'
# (steal) simply reports INFO/not-applicable when absent, which is
# expected and normal on non-virtualised hosts.
#
#   CPU_IOWAIT - high iowait means processes are stuck waiting on
#     storage, not CPU - a common but easy-to-miss root cause of a
#     "slow" server.
#   CPU_UTIL   - overall busy % (100 - idle). Load average alone
#     doesn't tell you how busy the CPUs actually are relative to
#     core count in the way a direct utilisation reading does.
#   CPU_STEAL  - only meaningful on VMs: time this VM's vCPU wanted
#     to run but the hypervisor gave the physical core to a different
#     guest instead. Sustained steal above a few percent means the
#     host is oversubscribed and no amount of tuning inside this VM
#     will fix it. Thresholds (5%/10%) follow common virtualisation
#     monitoring guidance (e.g. ServerScout, ScoutAPM's steal-time
#     write-ups), not a single Red Hat-specific number.
#############################################################################

check_cpu_stats() {

    if ! command -v vmstat >/dev/null 2>&1
    then
        report "CPU_IOWAIT" "INFO" "vmstat not installed"
        report "CPU_UTIL" "INFO" "vmstat not installed"
        report "CPU_STEAL" "INFO" "vmstat not installed"
        return 0
    fi

    local RAW WA_COL ID_COL ST_COL IOWAIT IDLE STEAL UTIL STATUS

    RAW=$(vmstat 1 2 2>/dev/null)
    WA_COL=$(awk 'NR==2 { for (i=1;i<=NF;i++) if ($i=="wa") print i }' <<< "$RAW")
    ID_COL=$(awk 'NR==2 { for (i=1;i<=NF;i++) if ($i=="id") print i }' <<< "$RAW")
    ST_COL=$(awk 'NR==2 { for (i=1;i<=NF;i++) if ($i=="st") print i }' <<< "$RAW")

    # --- CPU_IOWAIT ---
    if [[ -z "$WA_COL" ]]
    then
        report "CPU_IOWAIT" "INFO" "Could not locate wa column in vmstat output"
    else
        IOWAIT=$(awk -v col="$WA_COL" 'NR==4 {print $col}' <<< "$RAW")
        if [[ -z "$IOWAIT" ]]
        then
            report "CPU_IOWAIT" "INFO" "N/A"
        else
            STATUS="PASS"
            if [[ "$IOWAIT" -ge "$IOWAIT_CRIT_THRESHOLD" ]]
            then
                STATUS="CRITICAL"
            elif [[ "$IOWAIT" -ge "$IOWAIT_WARN_THRESHOLD" ]]
            then
                STATUS="WARNING"
            fi
            report "CPU_IOWAIT" "$STATUS" "${IOWAIT}%"
        fi
    fi

    # --- CPU_UTIL (100 - idle) ---
    if [[ -z "$ID_COL" ]]
    then
        report "CPU_UTIL" "INFO" "Could not locate id column in vmstat output"
    else
        IDLE=$(awk -v col="$ID_COL" 'NR==4 {print $col}' <<< "$RAW")
        if [[ -z "$IDLE" ]]
        then
            report "CPU_UTIL" "INFO" "N/A"
        else
            UTIL=$((100 - IDLE))
            STATUS="PASS"
            if [[ "$UTIL" -ge "$CPU_UTIL_CRIT_THRESHOLD" ]]
            then
                STATUS="CRITICAL"
            elif [[ "$UTIL" -ge "$CPU_UTIL_WARN_THRESHOLD" ]]
            then
                STATUS="WARNING"
            fi
            report "CPU_UTIL" "$STATUS" "${UTIL}%"
        fi
    fi

    # --- CPU_STEAL ---
    if [[ -z "$ST_COL" ]]
    then
        report "CPU_STEAL" "INFO" "Not reported (bare metal, or vmstat has no st column)"
    else
        STEAL=$(awk -v col="$ST_COL" 'NR==4 {print $col}' <<< "$RAW")
        if [[ -z "$STEAL" ]]
        then
            report "CPU_STEAL" "INFO" "N/A"
        else
            STATUS="PASS"
            if [[ "$STEAL" -ge "$CPU_STEAL_CRIT_THRESHOLD" ]]
            then
                STATUS="CRITICAL"
            elif [[ "$STEAL" -ge "$CPU_STEAL_WARN_THRESHOLD" ]]
            then
                STATUS="WARNING"
            fi
            report "CPU_STEAL" "$STATUS" "${STEAL}%"
        fi
    fi
}

#############################################################################
# Inode Usage
#
# A classic overlooked RHEL issue: a filesystem can hit 100% inode
# usage (often /var or /tmp full of many small files) and start
# refusing writes even though 'df -h' still shows plenty of free space.
#############################################################################

check_inodes() {

    local WARNINGS

    WARNINGS=$(df -PTi 2>/dev/null | awk -v threshold="$INODE_WARN_THRESHOLD" '
    NR > 1 {
        pct=$6
        gsub("%","",pct)
        if (pct == "-" || pct == "")
            next
        if (pct+0 >= threshold)
            printf "%s=%s%%\n", $7, pct
    }' | paste -sd ";" -)

    if [[ -z "$WARNINGS" ]]
    then
        report "INODE_USAGE" "PASS" "All filesystems below ${INODE_WARN_THRESHOLD}% inodes used"
    else
        report "INODE_USAGE" "WARNING" "$WARNINGS"
    fi
}

#############################################################################
# Disk I/O Performance (per device)
#
# The existing CPU_IOWAIT check above shows system-wide "waiting on
# storage" but can't say which device is responsible. iostat (sysstat
# package, RHEL BaseOS/AppStream) gives per-device %util and average
# wait (await, ms) - the two numbers Red Hat's own Performance Tuning
# Guide points to for diagnosing a specific slow disk rather than just
# knowing "storage is slow somewhere". Reports only the single worst
# device by %util to keep the row count sane on hosts with many
# block devices; the full per-device table is still in iostat's own
# output if deeper investigation is needed on the host directly.
#############################################################################

check_disk_io() {

    if ! command -v iostat >/dev/null 2>&1
    then
        report "DISK_IO" "INFO" "iostat not installed (sysstat package)"
        return 0
    fi

    local RAW UTIL_COL AWAIT_COL DEV UTIL AWAIT LINE
    local WORST_DEV="" WORST_UTIL=0 WORST_AWAIT="N/A" STATUS

    # -x extended stats, -z skip all-zero devices, "1 2" -> two 1-second
    # samples like the vmstat checks above; only the second (real-time)
    # sample's device block is used below, discarding the first
    # since-boot-average one.
    RAW=$(iostat -xz 1 2 2>/dev/null)

    UTIL_COL=$(awk '/^Device/ { for (i=1;i<=NF;i++) if ($i=="%util") col=i; print col; exit }' <<< "$RAW")

    # await column name differs across sysstat versions - newer releases
    # split read/write await ('w_await'); older releases have one 'await'.
    AWAIT_COL=$(awk '/^Device/ { for (i=1;i<=NF;i++) if ($i=="await") col=i; print col; exit }' <<< "$RAW")
    if [[ -z "$AWAIT_COL" ]]
    then
        AWAIT_COL=$(awk '/^Device/ { for (i=1;i<=NF;i++) if ($i=="w_await") col=i; print col; exit }' <<< "$RAW")
    fi

    if [[ -z "$UTIL_COL" ]]
    then
        report "DISK_IO" "INFO" "Could not locate %util column in iostat output"
        return 0
    fi

    # Take device rows from the SECOND "Device" block only (the
    # real-time sample), stopping at the next blank line.
    while IFS= read -r LINE
    do
        DEV=$(awk '{print $1}' <<< "$LINE")
        UTIL=$(awk -v c="$UTIL_COL" '{print $c}' <<< "$LINE")
        AWAIT="N/A"
        [[ -n "$AWAIT_COL" ]] && AWAIT=$(awk -v c="$AWAIT_COL" '{print $c}' <<< "$LINE")

        [[ -z "$DEV" || -z "$UTIL" ]] && continue

        if awk -v u="$UTIL" -v w="$WORST_UTIL" 'BEGIN{exit !(u>w)}'
        then
            WORST_UTIL="$UTIL"
            WORST_DEV="$DEV"
            WORST_AWAIT="$AWAIT"
        fi

    done < <(awk '
        /^Device/ { blocks++; inblock=1; next }
        /^$/ { inblock=0; next }
        inblock && blocks==2 { print }
    ' <<< "$RAW")

    if [[ -z "$WORST_DEV" ]]
    then
        report "DISK_IO" "INFO" "No block devices reported by iostat"
        return 0
    fi

    STATUS="PASS"
    if awk -v v="$WORST_UTIL" -v t="$DISK_UTIL_CRIT_THRESHOLD" 'BEGIN{exit !(v>=t)}'
    then
        STATUS="CRITICAL"
    elif awk -v v="$WORST_UTIL" -v t="$DISK_UTIL_WARN_THRESHOLD" 'BEGIN{exit !(v>=t)}'
    then
        STATUS="WARNING"
    fi

    report "DISK_IO" "$STATUS" "${WORST_DEV}: %util=${WORST_UTIL} await=${WORST_AWAIT}ms (worst device)"
}

#############################################################################
# Unexpectedly Read-Only Filesystems
#
# THE classic silent RHEL failure mode: after a disk I/O error, the
# kernel remounts the affected filesystem read-only as a data-loss
# safety net. Everything looks "up" - load is normal, the service is
# still running - but every write from that point on fails silently
# until someone notices. Scoped to local, real block-device-backed
# filesystem types (xfs/ext4/ext3/ext2/btrfs/vfat); network filesystems
# (nfs/nfs4) are deliberately excluded since a read-only NFS export is
# often an intentional server-side choice, not a fault on this host.
# RO_EXCLUDE_MOUNTS (top of script) can list any mountpoint your own
# hardening baseline deliberately mounts ro (e.g. a CIS profile that
# mounts /boot read-only outside kernel updates).
#############################################################################

check_readonly_filesystems() {

    local DEV MP FSTYPE OPTS REST
    local RO_LIST="" COUNT=0

    while IFS=' ' read -r DEV MP FSTYPE OPTS REST
    do
        case "$FSTYPE" in
            xfs|ext2|ext3|ext4|btrfs|vfat) ;;
            *) continue ;;
        esac

        case " $RO_EXCLUDE_MOUNTS " in
            *" $MP "*) continue ;;
        esac

        case ",$OPTS," in
            *,ro,*)
                COUNT=$((COUNT + 1))
                RO_LIST="${RO_LIST:+$RO_LIST; }${MP} (${FSTYPE})"
                ;;
        esac

    done < /proc/mounts

    if [[ "$COUNT" -eq 0 ]]
    then
        report "FS_READONLY" "PASS" "0 filesystems unexpectedly read-only"
    else
        report "FS_READONLY" "CRITICAL" "$RO_LIST"
    fi
}

#############################################################################
# LVM Thin-Pool Utilisation
#
# Distinct from df_check.sh's filesystem-level %used: a thin pool can
# run out of backing space while every filesystem on top of it still
# reports plenty of free room, because thin provisioning over-commits
# by design. Skips gracefully (INFO) on hosts with no thin pools -
# LVM itself may still be in use for plain (non-thin) volumes, which
# this check intentionally doesn't flag since they're already covered
# by ordinary filesystem-capacity monitoring.
#############################################################################

check_lvm_thinpool() {

    if ! command -v lvs >/dev/null 2>&1
    then
        report "LVM_THINPOOL" "INFO" "lvs not installed (lvm2 package)"
        return 0
    fi

    local RAW LINE LVNAME VG DATAPCT METAPCT STATUS FOUND=0

    RAW=$(lvs --noheadings --separator '|' -o lv_name,vg_name,data_percent,metadata_percent 2>/dev/null)

    if [[ -z "$RAW" ]]
    then
        report "LVM_THINPOOL" "INFO" "No LVM thin pools in use"
        return 0
    fi

    while IFS='|' read -r LVNAME VG DATAPCT METAPCT
    do
        LVNAME=$(awk '{$1=$1};1' <<< "$LVNAME")
        VG=$(awk '{$1=$1};1' <<< "$VG")
        DATAPCT=$(awk '{$1=$1};1' <<< "$DATAPCT")
        METAPCT=$(awk '{$1=$1};1' <<< "$METAPCT")

        # Non-thin LVs report an empty data_percent - not this check's
        # concern (already covered by ordinary filesystem monitoring).
        [[ -z "$DATAPCT" ]] && continue

        FOUND=1
        STATUS="PASS"

        if awk -v d="$DATAPCT" -v t="$LVM_THINPOOL_CRIT_THRESHOLD" 'BEGIN{exit !(d>=t)}' ||
           awk -v m="${METAPCT:-0}" -v t="$LVM_THINPOOL_CRIT_THRESHOLD" 'BEGIN{exit !(m>=t)}'
        then
            STATUS="CRITICAL"
        elif awk -v d="$DATAPCT" -v t="$LVM_THINPOOL_WARN_THRESHOLD" 'BEGIN{exit !(d>=t)}' ||
             awk -v m="${METAPCT:-0}" -v t="$LVM_THINPOOL_WARN_THRESHOLD" 'BEGIN{exit !(m>=t)}'
        then
            STATUS="WARNING"
        fi

        report "LVM_THINPOOL" "$STATUS" "${VG}/${LVNAME}: data=${DATAPCT}% metadata=${METAPCT:-0}%"

    done <<< "$RAW"

    if [[ "$FOUND" -eq 0 ]]
    then
        report "LVM_THINPOOL" "INFO" "No LVM thin pools in use"
    fi
}

#############################################################################
# Software RAID (mdadm) Degradation
#
# Scoped to Linux software RAID (/proc/mdstat) only - hardware RAID
# controllers need vendor-specific tooling (MegaCli/storcli/ssacli/
# perccli etc.) that isn't a standard RHEL BaseOS/AppStream package,
# so is out of scope here. Skips gracefully (INFO) on hosts with no
# md arrays at all. Multipath is deliberately NOT covered - already
# dropped previously as low-signal for this fleet (see role README).
#############################################################################

check_raid_status() {

    if [[ ! -r /proc/mdstat ]]
    then
        report "RAID_STATUS" "INFO" "/proc/mdstat not present (no software RAID)"
        return 0
    fi

    local ARRAYS DEGRADED=""

    ARRAYS=$(awk '/^md[0-9]/ {print $1}' /proc/mdstat)

    if [[ -z "$ARRAYS" ]]
    then
        report "RAID_STATUS" "INFO" "No active md arrays"
        return 0
    fi

    # A healthy array's status line lists every member as up, e.g.
    # "[UUUU]"; a missing/failed member shows as "_", e.g. "[UU_U]".
    DEGRADED=$(awk '/\[[U_]+\]/ && /_/ {print prev": "$0} {prev=$1}' /proc/mdstat | paste -sd ";" -)

    if [[ -z "$DEGRADED" ]]
    then
        report "RAID_STATUS" "PASS" "All md arrays healthy ($ARRAYS)"
    else
        report "RAID_STATUS" "CRITICAL" "$DEGRADED"
    fi
}

#############################################################################
# OOM-Killer Events (last 24h)
#
# Covers both classic system-wide kernel OOM kills and cgroup-scoped
# OOM events (systemd-oomd, or a container/service hitting its own
# memory.max) - the two produce different journal phrasing, and a
# cgroup limit being hit is just as real an OOM event as a system-wide
# one, even though total system memory may still look fine.
#############################################################################

check_oom_events() {

    local COUNT

    if ! command -v journalctl >/dev/null 2>&1
    then
        report "OOM_EVENTS_24H" "INFO" "journalctl not available"
        return 0
    fi

    COUNT=$(journalctl --since "24 hours ago" --no-pager -o cat 2>/dev/null |
            grep -Eic "out of memory|oom-killer|killed process|oom_kill|memory cgroup out of memory")

    if [[ "$COUNT" -eq 0 ]]
    then
        report "OOM_EVENTS_24H" "PASS" "0"
    else
        report "OOM_EVENTS_24H" "CRITICAL" "$COUNT"
    fi
}

#############################################################################
# Recent Kernel/Hardware Errors (last 6h)
#############################################################################

check_kernel_errors() {

    local RAW COUNT

    if ! command -v journalctl >/dev/null 2>&1
    then
        report "KERNEL_ERRORS_6H" "INFO" "journalctl not available"
        return 0
    fi

    RAW=$(journalctl -k -p err --since "6 hours ago" --no-pager -o cat 2>/dev/null)

    if [[ -z "$RAW" ]]
    then
        COUNT=0
    else
        COUNT=$(grep -vc '^--' <<< "$RAW")
    fi

    if [[ "$COUNT" -eq 0 ]]
    then
        report "KERNEL_ERRORS_6H" "PASS" "0"
    else
        report "KERNEL_ERRORS_6H" "WARNING" "$COUNT"
    fi
}

#############################################################################
# SELinux AVC Denials (last 6h)
#
# Overview's SELINUX column tracks enforcing/permissive MODE only - a
# static config fact. This is the dynamic signal that mode alone can't
# show: is SELinux actually blocking something right now. The 6-hour
# window matches this report's own cron cadence (every 6h, see role
# README) so there's no gap between runs without double-counting the
# same denial across two consecutive reports.
#############################################################################

check_selinux_avc() {

    if ! command -v ausearch >/dev/null 2>&1
    then
        report "SELINUX_AVC_6H" "INFO" "ausearch not installed (audit package)"
        return 0
    fi

    if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce 2>/dev/null)" == "Disabled" ]]
    then
        report "SELINUX_AVC_6H" "INFO" "SELinux disabled"
        return 0
    fi

    local START_DATE START_TIME RAW COUNT

    START_DATE=$(date -d "6 hours ago" +%m/%d/%Y)
    START_TIME=$(date -d "6 hours ago" +%H:%M:%S)

    RAW=$(ausearch -m avc -ts "$START_DATE" "$START_TIME" -i 2>/dev/null)
    COUNT=$(grep -c '^type=AVC' <<< "$RAW")

    if [[ "$COUNT" -eq 0 ]]
    then
        report "SELINUX_AVC_6H" "PASS" "0"
    else
        report "SELINUX_AVC_6H" "WARNING" "$COUNT denial(s) in last 6h"
    fi
}

#############################################################################
# Leftover Crash Dumps
#
# A non-empty /var/crash is direct evidence a kernel panic happened at
# some point - worth surfacing even if the host has been up and quiet
# since, because nobody may have gone back to actually read the dump.
# Pairs with the KDUMP check below: kdump active + a dump present here
# means something crashed AND was captured for investigation; kdump
# inactive means a future crash like this would leave nothing at all.
#############################################################################

check_crash_dumps() {

    if [[ ! -d /var/crash ]]
    then
        report "CRASH_DUMPS" "INFO" "/var/crash not present (kdump not configured to dump there)"
        return 0
    fi

    local COUNT

    COUNT=$(find /var/crash -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)

    if [[ "$COUNT" -eq 0 ]]
    then
        report "CRASH_DUMPS" "PASS" "0 crash dumps present"
    else
        report "CRASH_DUMPS" "WARNING" "$COUNT crash dump(s) present in /var/crash - review and clear once investigated"
    fi
}

#############################################################################
# Processes Stuck in D-State
#
# Uninterruptible sleep almost always means a hung disk or NFS mount -
# a far more actionable signal than a zombie count (zombies are usually
# harmless orphaned children waiting to be reaped).
#############################################################################

check_d_state_processes() {

    local COUNT

    COUNT=$(ps -eo stat= | grep -c '^D')

    if [[ "$COUNT" -eq 0 ]]
    then
        report "PROC_D_STATE" "PASS" "0"
    else
        report "PROC_D_STATE" "WARNING" "$COUNT"

        ps -eo pid,stat,comm | awk -v host="$HOST" '
        $2 ~ /^D/ {
            printf "%s|D_STATE_PROC|WARNING|PID=%s CMD=%s\n", host, $1, $3
        }'
    fi
}

#############################################################################
# Network Interface Errors/Drops/Overruns and Carrier State
#
# Iterates every interface under /sys/class/net (excluding loopback).
# RX/TX errors, drops, or overruns are an early sign of a failing
# link, cable, or driver issue that a simple "is the interface up"
# check won't catch. Also flags any interface that is administratively
# UP but has no carrier (cable unplugged, switch port down, etc.) -
# distinct from the error counters, and just as easy to miss otherwise.
#
# The error/drop/overrun counters are cumulative since the interface
# was last brought up (typically since boot) and essentially never
# reset themselves - a single stray packet dropped once during link
# negotiation weeks ago would otherwise pin this check to WARNING
# forever. NET_ERR_WARN_THRESHOLD (default 10) absorbs that kind of
# one-off noise; anything at or above it is worth a look. Still
# reports the exact counts for every interface with any nonzero value,
# even if under threshold, so the raw numbers stay visible on the
# dashboard - only the PASS/WARNING classification is threshold-gated.
#############################################################################

check_network_errors() {

    local IFACE STATS RXERR TXERR RXDROP TXDROP RXOVER TXOVER TOTAL OPER FLAGS
    local WORST_TOTAL=0
    local DETAILS=""
    local NO_CARRIER=""

    while IFS= read -r IFACE
    do
        [[ "$IFACE" == "lo" ]] && continue

        STATS=$(ip -s link show "$IFACE" 2>/dev/null)

        RXERR=$(awk '/^ *RX:/ {getline; print $3; exit}' <<< "$STATS")
        RXDROP=$(awk '/^ *RX:/ {getline; print $4; exit}' <<< "$STATS")
        RXOVER=$(awk '/^ *RX:/ {getline; print $5; exit}' <<< "$STATS")
        TXERR=$(awk '/^ *TX:/ {getline; print $3; exit}' <<< "$STATS")
        TXDROP=$(awk '/^ *TX:/ {getline; print $4; exit}' <<< "$STATS")
        TXOVER=$(awk '/^ *TX:/ {getline; print $5; exit}' <<< "$STATS")

        TOTAL=$(( ${RXERR:-0} + ${RXDROP:-0} + ${RXOVER:-0} + ${TXERR:-0} + ${TXDROP:-0} + ${TXOVER:-0} ))

        if [[ "$TOTAL" -gt 0 ]]
        then
            DETAILS="${DETAILS:+$DETAILS; }${IFACE}: rx_err=${RXERR:-0} rx_drop=${RXDROP:-0} rx_over=${RXOVER:-0} tx_err=${TXERR:-0} tx_drop=${TXDROP:-0} tx_over=${TXOVER:-0}"
            [[ "$TOTAL" -gt "$WORST_TOTAL" ]] && WORST_TOTAL="$TOTAL"
        fi

        # Administratively UP but no carrier (cable/switch-port down).
        # Read sysfs directly rather than parsing 'ip link show' text -
        # IFF_UP is bit 0x1 of the interface's /sys .../flags value, so
        # operstate=="down" only counts as a fault when that bit is
        # also set (an interface the operator deliberately brought
        # down administratively also reports operstate "down", but
        # isn't a cable/switch fault and shouldn't be flagged).
        if [[ -r "/sys/class/net/${IFACE}/operstate" && -r "/sys/class/net/${IFACE}/flags" ]]
        then
            OPER=$(< "/sys/class/net/${IFACE}/operstate")
            FLAGS=$(< "/sys/class/net/${IFACE}/flags")

            if [[ "$OPER" == "down" ]] && (( (FLAGS) & 0x1 ))
            then
                NO_CARRIER="${NO_CARRIER:+$NO_CARRIER, }${IFACE}"
            fi
        fi

    done < <(ls /sys/class/net/ 2>/dev/null)

    if [[ -z "$DETAILS" ]]
    then
        report "NET_ERRORS" "PASS" "0 errors/drops/overruns on all interfaces"
    elif [[ "$WORST_TOTAL" -ge "$NET_ERR_WARN_THRESHOLD" ]]
    then
        report "NET_ERRORS" "WARNING" "$DETAILS"
    else
        report "NET_ERRORS" "PASS" "$DETAILS (below ${NET_ERR_WARN_THRESHOLD}-count warn threshold)"
    fi

    if [[ -z "$NO_CARRIER" ]]
    then
        report "NET_CARRIER" "PASS" "All administratively-up interfaces have carrier"
    else
        report "NET_CARRIER" "WARNING" "No carrier (cable/switch port down?): $NO_CARRIER"
    fi
}

#############################################################################
# TCP/Socket Health
#
# ss (iproute package) is Red Hat's own recommended replacement for
# netstat on modern RHEL. Reports the total TCP socket count from ss's
# own summary - a coarse but cheap-and-reliable signal for socket
# exhaustion (e.g. a leaking connection pool, a runaway fork bomb of
# short-lived connections, or a service stuck accumulating CLOSE_WAIT
# sockets). Per-state breakdown (TIME_WAIT/CLOSE_WAIT counts,
# retransmit rates, etc.) needs deeper /proc/net/netstat parsing that
# is more fragile across kernel versions - kept out of this pass in
# favour of the one number most directly tied to real exhaustion risk.
#############################################################################

check_socket_health() {

    if ! command -v ss >/dev/null 2>&1
    then
        report "SOCKET_HEALTH" "INFO" "ss not installed (iproute package)"
        return 0
    fi

    local TCP_TOTAL STATUS

    # 'ss -s' summary line looks like: "TCP:   10 (estab 3, closed 4, ...)"
    # - field 1 is the literal "TCP" label, field 2 is the total count
    # we want; field 3 onward is the "(estab ..." breakdown text.
    TCP_TOTAL=$(ss -s 2>/dev/null | awk -F'[ :]+' '/^TCP:/ {print $2; exit}')

    if [[ -z "$TCP_TOTAL" || ! "$TCP_TOTAL" =~ ^[0-9]+$ ]]
    then
        report "SOCKET_HEALTH" "INFO" "Could not parse TCP socket count from ss -s"
        return 0
    fi

    STATUS="PASS"
    if [[ "$TCP_TOTAL" -ge "$SOCKET_WARN_THRESHOLD" ]]
    then
        STATUS="WARNING"
    fi

    report "SOCKET_HEALTH" "$STATUS" "${TCP_TOTAL} TCP sockets"
}

#############################################################################
# Logged In Users
#############################################################################

check_users() {

    local USERS

    USERS=$(who | awk '{print $1}' | sort -u | paste -sd "," -)

    if [[ -z "$USERS" ]]
    then
        USERS="NONE"
    fi

    report "LOGGED_ON_USERS" "INFO" "$USERS"
}

#############################################################################
# Service Restart Counts ("Flapping")
#
# NOT general failed-unit detection - Overview's FAILED_SVC_CNT/
# FAILED_SVCS (roles/hostinfo) already owns that. This is a narrower,
# different signal: a service that keeps DYING AND RESTARTING (systemd
# brings it back up each time, so it never shows as "failed") is
# invisible to a simple active/inactive check but is still a real
# problem. Scoped to the same small set of daemons this repo already
# tracks elsewhere (sshd/firewalld/chronyd/auditd/crond/sssd) rather
# than every unit on the system, to avoid reopening the "service
# monitoring lives in one place" duplication this repo deliberately
# resolved. NRestarts is cumulative since boot, so - same reasoning as
# the network error counters above - this flags a count AT OR ABOVE a
# threshold rather than needing a delta between runs.
#############################################################################

check_service_restarts() {

    local SERVICE COUNT STATUS
    local SERVICES="sshd firewalld chronyd auditd crond sssd"

    for SERVICE in $SERVICES
    do
        COUNT=$(systemctl show "$SERVICE" -p NRestarts --value 2>/dev/null)

        if [[ -z "$COUNT" ]]
        then
            report "SVC_RESTARTS_${SERVICE^^}" "INFO" "$SERVICE not present"
            continue
        fi

        STATUS="PASS"
        if [[ "$COUNT" -ge "$SERVICE_RESTART_WARN_THRESHOLD" ]]
        then
            STATUS="WARNING"
        fi

        report "SVC_RESTARTS_${SERVICE^^}" "$STATUS" "${COUNT} restart(s) since boot"
    done
}

#############################################################################
# Auth & Identity: Failed SSH Logins (last 24h)
#############################################################################

check_ssh_failed_logins() {

    local COUNT

    if ! command -v journalctl >/dev/null 2>&1
    then
        report "SSH_FAILED_24H" "INFO" "journalctl not available"
        return 0
    fi

    COUNT=$(journalctl -u sshd --since "24 hours ago" --no-pager -o cat 2>/dev/null |
            grep -Eic "failed password|failed publickey|authentication failure")

    if [[ "$COUNT" -eq 0 ]]
    then
        report "SSH_FAILED_24H" "PASS" "0"
    elif [[ "$COUNT" -lt "$SSH_FAILED_WARN_THRESHOLD" ]]
    then
        report "SSH_FAILED_24H" "INFO" "$COUNT"
    else
        report "SSH_FAILED_24H" "WARNING" "$COUNT"
    fi
}

#############################################################################
# Auth & Identity: Failed sudo Attempts (last 24h)
#############################################################################

check_sudo_failed() {

    local COUNT

    if ! command -v journalctl >/dev/null 2>&1
    then
        report "SUDO_FAILED_24H" "INFO" "journalctl not available"
        return 0
    fi

    COUNT=$(journalctl SYSLOG_IDENTIFIER=sudo --since "24 hours ago" --no-pager -o cat 2>/dev/null |
            grep -ci "authentication failure")

    if [[ "$COUNT" -eq 0 ]]
    then
        report "SUDO_FAILED_24H" "PASS" "0"
    else
        report "SUDO_FAILED_24H" "WARNING" "$COUNT"
    fi
}

#############################################################################
# Auth & Identity: Locked Local Accounts
#
# Only covers LOCAL accounts (/etc/shadow) - IdM/domain user lockouts
# are tracked by SSSD/pam_faillock, not local shadow, and aren't
# reflected here. Filtered to UID >= 1000 with a real login shell, so
# routine locked system/service accounts (the RHEL default) don't
# generate noise.
#############################################################################

check_locked_accounts() {

    local LOCKED_USERNAMES USERNAME UID_VAL SHELL
    local REAL_LOCKED=""
    local COUNT=0

    LOCKED_USERNAMES=$(awk -F: '$2 ~ /^!/ {print $1}' /etc/shadow 2>/dev/null)

    while IFS= read -r USERNAME
    do
        [[ -z "$USERNAME" ]] && continue

        UID_VAL=$(id -u "$USERNAME" 2>/dev/null)
        [[ -z "$UID_VAL" ]] && continue
        [[ "$UID_VAL" -lt 1000 ]] && continue

        SHELL=$(getent passwd "$USERNAME" | cut -d: -f7)
        case "$SHELL" in
            */nologin|*/false) continue ;;
        esac

        COUNT=$((COUNT + 1))
        REAL_LOCKED="${REAL_LOCKED:+$REAL_LOCKED,}$USERNAME"

    done <<< "$LOCKED_USERNAMES"

    if [[ "$COUNT" -eq 0 ]]
    then
        report "LOCKED_ACCOUNTS" "PASS" "0"
    else
        report "LOCKED_ACCOUNTS" "WARNING" "$COUNT ($REAL_LOCKED)"
    fi
}

#############################################################################
# Auth & Identity: SSSD Daemon Status
#
# Distinct from Overview's REALM column (which just names the domain) -
# this is whether the client daemon that actually talks to it is up.
#############################################################################

check_sssd_status() {

    local STATE

    STATE=$(systemctl is-active sssd 2>/dev/null)

    if [[ -z "$STATE" || "$STATE" == "unknown" ]]
    then
        report "SSSD" "INFO" "not installed"
    elif [[ "$STATE" == "active" ]]
    then
        report "SSSD" "PASS" "active"
    else
        report "SSSD" "WARNING" "$STATE"
    fi
}

#############################################################################
# Auth & Identity: IdM/LDAP Domain Reachability
#
# Skips gracefully (INFO) on hosts that aren't domain-joined, or where
# sssctl (sssd-tools) isn't installed - this is a live online/offline
# check, not just "is a domain configured".
#############################################################################

check_idm_reachability() {

    local DOMAIN STATE

    DOMAIN=$(realm list 2>/dev/null | awk '/^[A-Za-z0-9.-]+$/ {print; exit}')

    if [[ -z "$DOMAIN" ]]
    then
        report "IDM_REACHABLE" "INFO" "Not domain-joined"
        return 0
    fi

    if ! command -v sssctl >/dev/null 2>&1
    then
        report "IDM_REACHABLE" "INFO" "sssctl not installed (domain=$DOMAIN)"
        return 0
    fi

    STATE=$(sssctl domain-status "$DOMAIN" --online 2>/dev/null |
            awk -F': ' 'tolower($1) ~ /online status/ {print $2; exit}')

    if [[ "$STATE" == "Online" ]]
    then
        report "IDM_REACHABLE" "PASS" "$DOMAIN online"
    else
        report "IDM_REACHABLE" "WARNING" "$DOMAIN ${STATE:-unreachable}"
    fi
}

#############################################################################
# Auth & Identity: Kerberos KDC Reachability
#
# Uses the host's own keytab (present on any IdM/AD-joined host) to
# kinit as the host principal - a real round trip to the KDC, not just
# a config-file check - then immediately destroys the ticket. Skips
# gracefully on hosts with no keytab or no krb5-workstation installed.
#############################################################################

check_krb5() {

    local PRINC CCACHE

    if [[ ! -f /etc/krb5.keytab ]] ||
       ! command -v kinit >/dev/null 2>&1 ||
       ! command -v klist >/dev/null 2>&1
    then
        report "KRB5" "INFO" "No host keytab / krb5-workstation not installed"
        return 0
    fi

    PRINC=$(klist -k /etc/krb5.keytab 2>/dev/null | awk '$NF ~ /@/ {print $NF; exit}')

    if [[ -z "$PRINC" ]]
    then
        report "KRB5" "WARNING" "Could not read host principal from keytab"
        return 0
    fi

    CCACHE=$(mktemp)

    if KRB5CCNAME="$CCACHE" kinit -k -t /etc/krb5.keytab "$PRINC" >/dev/null 2>&1
    then
        report "KRB5" "PASS" "KDC reachable ($PRINC)"
    else
        report "KRB5" "WARNING" "kinit failed for $PRINC - KDC unreachable or keytab invalid"
    fi

    KRB5CCNAME="$CCACHE" kdestroy >/dev/null 2>&1
    rm -f "$CCACHE"
}

#############################################################################
# Auth & Identity: DNS Self-Resolution
#############################################################################

check_dns() {

    local FQDN

    FQDN=$(hostname -f 2>/dev/null)

    if [[ -z "$FQDN" ]]
    then
        report "DNS" "WARNING" "hostname -f returned nothing"
        return 0
    fi

    if getent hosts "$FQDN" >/dev/null 2>&1
    then
        report "DNS" "PASS" "Resolves own FQDN ($FQDN)"
    else
        report "DNS" "WARNING" "Cannot resolve own FQDN ($FQDN)"
    fi
}

#############################################################################
# Auth & Identity: Chrony Clock-Offset Magnitude
#
# Overview's NTP_SYNC is a binary synced/not-synced flag; this reports
# the actual drift size in milliseconds. Worth tracking specifically
# on this fleet given the IdM/Kerberos dependency (Kerberos itself
# tolerates up to 5 minutes of skew by default, so these thresholds
# are deliberately much tighter than that) and the centralized-logging
# work, where even sub-second drift across hosts can blur cross-host
# log correlation. Skips gracefully (INFO) if chronyd isn't installed
# or isn't running.
#############################################################################

check_chrony_offset() {

    if ! command -v chronyc >/dev/null 2>&1
    then
        report "CHRONY_OFFSET" "INFO" "chronyc not installed"
        return 0
    fi

    local RAW LINE OFFSET_S DIRECTION OFFSET_MS OFFSET_ABS_MS STATUS

    RAW=$(chronyc tracking 2>/dev/null)

    if [[ -z "$RAW" ]]
    then
        report "CHRONY_OFFSET" "INFO" "chronyc tracking returned nothing (chronyd not running?)"
        return 0
    fi

    LINE=$(awk -F': ' '/^System time/ {print $2}' <<< "$RAW")

    if [[ -z "$LINE" ]]
    then
        report "CHRONY_OFFSET" "INFO" "Could not find 'System time' line in chronyc tracking"
        return 0
    fi

    OFFSET_S=$(awk '{print $1}' <<< "$LINE")
    DIRECTION=$(awk '{print $3}' <<< "$LINE")

    if [[ -z "$OFFSET_S" ]]
    then
        report "CHRONY_OFFSET" "INFO" "Could not parse offset from chronyc tracking"
        return 0
    fi

    OFFSET_MS=$(awk -v s="$OFFSET_S" 'BEGIN { printf "%.1f", s * 1000 }')
    OFFSET_ABS_MS=$(awk -v v="$OFFSET_MS" 'BEGIN { print (v < 0) ? -v : v }')

    STATUS="PASS"
    if awk -v v="$OFFSET_ABS_MS" -v t="$CHRONY_OFFSET_CRIT_MS" 'BEGIN{exit !(v>=t)}'
    then
        STATUS="CRITICAL"
    elif awk -v v="$OFFSET_ABS_MS" -v t="$CHRONY_OFFSET_WARN_MS" 'BEGIN{exit !(v>=t)}'
    then
        STATUS="WARNING"
    fi

    report "CHRONY_OFFSET" "$STATUS" "${OFFSET_MS}ms ${DIRECTION:-N/A} of NTP time"
}

#############################################################################
# Auth & Identity: Certificate Expiry (certmonger)
#
# Lists EVERY certmonger-tracked certificate (IdM host/service certs,
# the IPA CA chain, dogtag subsystem certs, etc.) with its own row -
# not just the ones close to expiry - so the dashboard gives a full
# inventory, not just an exception list. Each row's own STATUS still
# reflects its risk (PASS / WARNING within CERT_EXPIRY_WARN_DAYS days /
# CRITICAL if already expired). Skips gracefully (INFO) if certmonger
# isn't installed or nothing is tracked ("Number of certificates and
# requests being tracked: 0." - true for every non-IdM-role host).
#
# Label picked per certificate, in order of preference: the NSSDB/FILE
# nickname if the cert has one (most do), else the subject CN (some
# FILE-backed certs like ra-agent.pem have no nickname at all), else
# the raw Request ID as a last-resort fallback.
#
# Date parsing: getcert prints "expires: 2028-05-24 22:30:09 BST"-
# style timestamps using whatever the HOST's own local timezone
# abbreviation is (BST, GMT, EST, etc.). GNU date's abbreviation-to-
# offset table is ambiguous and version/locale-dependent (e.g. "BST"
# can resolve to Bangladesh Standard Time instead of British Summer
# Time), so the trailing tz abbreviation is stripped and the naive
# timestamp is parsed in the script's own runtime timezone instead -
# which is correct, since certmonger wrote it using that same host's
# local clock in the first place.
#############################################################################

check_cert_expiry() {

    if ! command -v getcert >/dev/null 2>&1
    then
        report "CERT_EXPIRY" "INFO" "certmonger not installed"
        return 0
    fi

    local LINE REQID="" NICKNAME="" SUBJECT_CN="" CERT_LOCATION="" LABEL
    local EXP_STR EXP_STR_NOTZ LAST_TOKEN EXP_EPOCH NOW DAYS_LEFT DAYS_AGO STATUS VALUE
    local FOUND=0

    NOW=$(date +%s)

    while IFS= read -r LINE
    do
        if [[ "$LINE" =~ Request\ ID\ \'([^\']+)\' ]]
        then
            REQID="${BASH_REMATCH[1]}"
            NICKNAME=""
            SUBJECT_CN=""
            CERT_LOCATION=""
            continue
        fi

        if [[ "$LINE" =~ ^[[:space:]]*certificate:.*nickname=\'([^\']+)\' ]]
        then
            NICKNAME="${BASH_REMATCH[1]}"
            continue
        elif [[ "$LINE" =~ ^[[:space:]]*certificate:.*nickname=\"([^\"]+)\" ]]
        then
            NICKNAME="${BASH_REMATCH[1]}"
            continue
        fi

        if [[ -z "$NICKNAME" && "$LINE" =~ ^[[:space:]]*certificate:.*location=\'([^\']+)\' ]]
        then
            CERT_LOCATION="${BASH_REMATCH[1]}"
            continue
        fi

        if [[ "$LINE" =~ subject:[[:space:]]*CN=([^,]+) ]]
        then
            SUBJECT_CN="${BASH_REMATCH[1]}"
            continue
        fi

        if [[ "$LINE" =~ expires:[[:space:]]*(.+)$ ]]
        then
            FOUND=1
            EXP_STR="${BASH_REMATCH[1]}"

            LAST_TOKEN="${EXP_STR##* }"
            if [[ "$LAST_TOKEN" =~ ^[A-Za-z]{2,6}$ ]]
            then
                EXP_STR_NOTZ="${EXP_STR% "$LAST_TOKEN"}"
            else
                EXP_STR_NOTZ="$EXP_STR"
            fi

            # Label preference: nickname (most certs have one) -> subject
            # CN, disambiguated with the cert file's basename when two
            # FILE-backed certs share the same CN (e.g. idm1's httpd.crt
            # and kdc.crt both have subject CN=idm1.lab.example.com) ->
            # raw Request ID as a last resort.
            if [[ -n "$NICKNAME" ]]
            then
                LABEL="$NICKNAME"
            elif [[ -n "$SUBJECT_CN" ]]
            then
                if [[ -n "$CERT_LOCATION" ]]
                then
                    LABEL="$SUBJECT_CN (${CERT_LOCATION##*/})"
                else
                    LABEL="$SUBJECT_CN"
                fi
            else
                LABEL="ReqID $REQID"
            fi

            EXP_EPOCH=$(date -d "$EXP_STR_NOTZ" +%s 2>/dev/null)

            if [[ -z "$EXP_EPOCH" ]]
            then
                report "CERT_EXPIRY" "WARNING" "$LABEL: could not parse expiry date '$EXP_STR'"
                continue
            fi

            if [[ "$EXP_EPOCH" -lt "$NOW" ]]
            then
                DAYS_AGO=$(( (NOW - EXP_EPOCH) / 86400 ))
                STATUS="CRITICAL"
                VALUE="$LABEL: EXPIRED $EXP_STR_NOTZ (${DAYS_AGO}d ago)"
            else
                DAYS_LEFT=$(( (EXP_EPOCH - NOW) / 86400 ))
                if [[ "$DAYS_LEFT" -lt "$CERT_EXPIRY_WARN_DAYS" ]]
                then
                    STATUS="WARNING"
                else
                    STATUS="PASS"
                fi
                VALUE="$LABEL: expires $EXP_STR_NOTZ (${DAYS_LEFT}d left)"
            fi

            report "CERT_EXPIRY" "$STATUS" "$VALUE"
        fi
    done < <(getcert list 2>/dev/null)

    if [[ "$FOUND" -eq 0 ]]
    then
        report "CERT_EXPIRY" "INFO" "No certificates tracked by certmonger"
    fi
}

#############################################################################
# Auth & Identity: kdump Crash-Capture Status
#
# Red Hat's own kernel crash dump mechanism. If it isn't active, a
# kernel panic on this host leaves nothing behind to investigate
# afterwards - the host just reboots and whatever caused the crash is
# gone. WARNING rather than CRITICAL, since some hosts (very small
# memory-constrained VMs where reserving crashkernel= memory isn't
# practical) may reasonably run without it.
#############################################################################

check_kdump() {

    if ! command -v systemctl >/dev/null 2>&1
    then
        report "KDUMP" "INFO" "systemctl not available"
        return 0
    fi

    local STATE

    STATE=$(systemctl is-active kdump 2>/dev/null)

    if [[ -z "$STATE" ]]
    then
        report "KDUMP" "INFO" "kdump service not present"
    elif [[ "$STATE" == "active" ]]
    then
        report "KDUMP" "PASS" "active"
    else
        report "KDUMP" "WARNING" "$STATE - a kernel panic would not be captured for investigation"
    fi
}

#############################################################################
# Main
#############################################################################

echo "HOSTNAME|CHECK|STATUS|VALUE"

# Resource pressure
check_load
check_memory
check_swap
check_swap_activity
check_cpu_stats
check_inodes

# Disk performance & storage health
check_disk_io
check_readonly_filesystems
check_lvm_thinpool
check_raid_status

# Logs
check_oom_events
check_kernel_errors
check_selinux_avc
check_crash_dumps

# Process / network
check_d_state_processes
check_network_errors
check_socket_health
check_users

# Services
check_service_restarts

# Auth & identity
check_ssh_failed_logins
check_sudo_failed
check_locked_accounts
check_sssd_status
check_idm_reachability
check_krb5
check_dns
check_chrony_offset
check_cert_expiry
check_kdump
