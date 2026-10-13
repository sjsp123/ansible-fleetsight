#!/usr/bin/env bash
#
# system_stats.sh
#
# Purpose:
#   Static hardware/firmware detail via dmidecode, plus CPU topology and
#   IP addressing. Deliberately does NOT repeat system-manufacturer,
#   system-product-name or system-serial-number - those already appear
#   as MFR/MODEL/SERIAL in the fleet Overview table. Everything below is
#   genuinely new information (BIOS, baseboard, chassis, system UUID/
#   version, CPU topology, per-DIMM memory detail, IP addressing).
#
# Output Format:
#   A leading HOSTNAME|CHECK|STATUS|VALUE table (as below), followed by
#   a blank line and then a raw remainder section with the full output
#   of 'nmcli device show' and 'ip -br a' - those don't fit the flat
#   table shape, so they're shown as free-form text underneath it
#   instead (same convention df_check/proc_check use for their own
#   non-tabular WARNINGS/SUMMARY sections).
#
# Status Values:
#   INFO
#   WARNING  (used only when a data source is unavailable)
#
# Supported:
#   RHEL 8 / RHEL 9 / RHEL 10
#

set -uo pipefail

export LC_ALL=C

HOST=$(hostname -s)

#############################################################################
# Configuration
#############################################################################

# Whether check_network() attempts an outbound curl to checkip.dyndns.org
# to report the host's public IP. Off by default - many networks (this
# lab included) route outbound HTTP through a proxy/firewall (e.g.
# Zscaler) that intercepts or otherwise interferes with the request,
# which is undesirable noise in a routine report even with the short
# --connect-timeout/--max-time already in place below. Set to true to
# re-enable it; PRIVATE_IPS (local interface addresses) is unaffected
# either way and always runs.
CHECK_PUBLIC_IP=false

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
# dmidecode availability
#
# Reading the DMI table requires root, which is fine here since this
# script always runs under 'become: true' in the playbook - but we still
# degrade gracefully (single WARNING line, script keeps running) rather
# than hard-exiting, so ad-hoc/manual runs as a non-root user still
# produce a usable report for the non-dmidecode sections. This check
# only sets a flag - it must not print anything before main echoes the
# HOSTNAME|CHECK|STATUS|VALUE header, since the dashboard's table
# parser requires that header to be the very first line of output.
#############################################################################

DMI_OK=1
DMI_UNAVAILABLE_REASON=""

if ! command -v dmidecode >/dev/null 2>&1
then
    DMI_UNAVAILABLE_REASON="dmidecode not installed"
    DMI_OK=0
elif [[ $(/usr/bin/id -u) != "0" ]]
then
    DMI_UNAVAILABLE_REASON="Not running as root - DMI table unreadable"
    DMI_OK=0
fi

dmi_field() {

    # Wrapper around 'dmidecode -s <keyword>' that:
    #   - returns "N/A" instead of an empty string when a field is
    #     absent (common on VMs/hypervisors for BIOS/baseboard fields)
    #   - drops noisy comment lines dmidecode -s sometimes prepends
    local KEYWORD="$1"
    local VALUE

    VALUE=$(dmidecode -s "$KEYWORD" 2>/dev/null | grep -v '^#' | head -1)

    if [[ -z "$VALUE" ]]
    then
        VALUE="N/A"
    fi

    echo "$VALUE"
}

#############################################################################
# System (version/UUID only - manufacturer/product/serial are already on
# the Overview page)
#############################################################################

check_system() {

    [[ "$DMI_OK" -eq 1 ]] || return 0

    report "SYS_VERSION" "INFO" "$(dmi_field system-version)"
    report "SYS_UUID" "INFO" "$(dmi_field system-uuid)"
}

#############################################################################
# BIOS
#############################################################################

check_bios() {

    [[ "$DMI_OK" -eq 1 ]] || return 0

    report "BIOS_VENDOR" "INFO" "$(dmi_field bios-vendor)"
    report "BIOS_VERSION" "INFO" "$(dmi_field bios-version)"
    report "BIOS_DATE" "INFO" "$(dmi_field bios-release-date)"
}

#############################################################################
# Baseboard
#############################################################################

check_baseboard() {

    [[ "$DMI_OK" -eq 1 ]] || return 0

    report "BASEBOARD_MFR" "INFO" "$(dmi_field baseboard-manufacturer)"
    report "BASEBOARD_PRODUCT" "INFO" "$(dmi_field baseboard-product-name)"
}

#############################################################################
# Chassis
#############################################################################

check_chassis() {

    [[ "$DMI_OK" -eq 1 ]] || return 0

    report "CHASSIS_TYPE" "INFO" "$(dmi_field chassis-type)"
}

#############################################################################
# CPU Topology
#############################################################################

check_cpu() {

    local MODEL SOCKETS CORES_PER_SOCKET THREADS_PER_CORE

    MODEL=$(lscpu | awk -F': +' '/^Model name:/ {print $2; exit}')
    SOCKETS=$(lscpu | awk -F': +' '/^Socket\(s\):/ {print $2; exit}')
    CORES_PER_SOCKET=$(lscpu | awk -F': +' '/^Core\(s\) per socket:/ {print $2; exit}')
    THREADS_PER_CORE=$(lscpu | awk -F': +' '/^Thread\(s\) per core:/ {print $2; exit}')

    report "CPU_MODEL" "INFO" "${MODEL:-N/A}"
    report "CPU_SOCKETS" "INFO" "${SOCKETS:-N/A}"
    report "CPU_CORES_PER_SOCKET" "INFO" "${CORES_PER_SOCKET:-N/A}"
    report "CPU_THREADS_PER_CORE" "INFO" "${THREADS_PER_CORE:-N/A}"
}

#############################################################################
# Memory - per-DIMM detail (dmidecode type 17 / "Memory Device")
#
# Only populated slots are listed individually; empty slots are still
# counted (MEM_SLOTS_TOTAL vs MEM_SLOTS_POPULATED) but not printed one
# by one, since "Locator=DIMM_B2 empty" repeated per slot adds noise
# without information.
#############################################################################

check_memory() {

    [[ "$DMI_OK" -eq 1 ]] || return 0

    local RAW TOTAL_SLOTS POPULATED_SLOTS

    RAW=$(dmidecode -t 17 2>/dev/null)
    TOTAL_SLOTS=$(grep -c '^Memory Device$' <<< "$RAW")

    if [[ "$TOTAL_SLOTS" -eq 0 ]]
    then
        report "MEM_SLOTS" "WARNING" "No memory device data returned by dmidecode"
        return 0
    fi

    POPULATED_SLOTS=0

    # dmidecode separates each DMI record with a blank line, so
    # 'awk -v RS=""' (paragraph mode) hands us one whole "Memory Device"
    # block per iteration. Each block is emitted NUL-terminated (not
    # newline-terminated) so its own embedded newlines survive the
    # 'read -d ""' below - and since this loop reads from a process
    # substitution rather than a pipe, POPULATED_SLOTS keeps counting in
    # this shell, not a throwaway subshell.
    while IFS= read -r -d '' BLOCK
    do
        local SIZE LOCATOR TYPE SPEED

        SIZE=$(grep -m1 '^[[:space:]]*Size:' <<< "$BLOCK" | cut -d: -f2- | sed 's/^ *//')

        if [[ -z "$SIZE" || "$SIZE" == "No Module Installed" ]]
        then
            continue
        fi

        POPULATED_SLOTS=$((POPULATED_SLOTS + 1))

        LOCATOR=$(grep -m1 '^[[:space:]]*Locator:' <<< "$BLOCK" | cut -d: -f2- | sed 's/^ *//')
        TYPE=$(grep -m1 '^[[:space:]]*Type:' <<< "$BLOCK" | cut -d: -f2- | sed 's/^ *//')
        SPEED=$(grep -m1 '^[[:space:]]*Speed:' <<< "$BLOCK" | cut -d: -f2- | sed 's/^ *//')

        report "MEM_SLOT" "INFO" "${LOCATOR:-N/A}: ${SIZE} ${TYPE:-N/A} @ ${SPEED:-N/A}"

    done < <(awk -v RS="" '/Memory Device/ { printf "%s%s", $0, "\0" }' <<< "$RAW")

    report "MEM_SLOTS_TOTAL" "INFO" "$TOTAL_SLOTS"
    report "MEM_SLOTS_POPULATED" "INFO" "$POPULATED_SLOTS"
}

#############################################################################
# IP Addressing
#############################################################################

check_network() {

    local PRIVATE_IPS PUBLIC_IP

    PRIVATE_IPS=$(ip -4 addr show 2>/dev/null |
                  awk '/inet /{print $2}' |
                  cut -d/ -f1 |
                  grep -v '^127\.' |
                  paste -sd "," -)

    report "PRIVATE_IPS" "INFO" "${PRIVATE_IPS:-NONE}"

    if [[ "$CHECK_PUBLIC_IP" != true ]]
    then
        report "PUBLIC_IP" "INFO" "Disabled (CHECK_PUBLIC_IP=false)"
        return
    fi

    # --max-time/--connect-timeout so an isolated lab host with no
    # internet egress fails fast instead of hanging the whole
    # collection run for curl's default timeout.
    PUBLIC_IP=$(curl -s --connect-timeout 2 --max-time 3 \
                checkip.dyndns.org 2>/dev/null |
                sed -e 's/.*Current IP Address: //' -e 's/<.*$//')

    if [[ -z "$PUBLIC_IP" ]]
    then
        report "PUBLIC_IP" "INFO" "N/A (unreachable or no internet egress)"
    else
        report "PUBLIC_IP" "INFO" "$PUBLIC_IP"
    fi
}

#############################################################################
# Network Interfaces (raw command output)
#
# nmcli device show and ip -br a are naturally multi-line, colon/space-
# delimited dumps that don't fit the flat HOSTNAME|CHECK|STATUS|VALUE
# table above - printed as free-form text instead, after a single blank
# separator line, and rendered by the dashboard as a preformatted block
# underneath the table (same convention as df_check/proc_check's own
# raw remainder sections). Both nmcli (NetworkManager) and ip (iproute)
# ship in RHEL's own BaseOS install; this only checks for them with
# 'command -v', never installs anything.
#############################################################################

check_network_interfaces() {

    echo ""
    echo "=== nmcli device show ==="
    if command -v nmcli >/dev/null 2>&1
    then
        nmcli device show 2>&1
    else
        echo "nmcli not installed"
    fi

    echo ""
    echo "=== ip -br a ==="
    if command -v ip >/dev/null 2>&1
    then
        ip -br a 2>&1
    else
        echo "ip (iproute) not installed"
    fi
}

#############################################################################
# Main
#############################################################################

echo "HOSTNAME|CHECK|STATUS|VALUE"

if [[ "$DMI_OK" -eq 0 ]]
then
    report "DMIDECODE" "WARNING" "$DMI_UNAVAILABLE_REASON"
fi

check_system
check_bios
check_baseboard
check_chassis
check_cpu
check_memory
check_network
check_network_interfaces

# No OVERALL_STATUS line here (unlike df_check/svr_check): the table
# above is the flat HOSTNAME|CHECK|STATUS|VALUE shape end-to-end, and a
# trailing 2-column summary row would break the dashboard's table
# renderer, which expects every row to have the same column count as
# the header. check_network_interfaces() (just called above) is
# exempt from this - it prints its raw, non-tabular remainder after
# the table on purpose; see that function's own header comment.
