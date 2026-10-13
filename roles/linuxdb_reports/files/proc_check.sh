#!/usr/bin/env bash
#
# process_check.sh
#
# Purpose:
#   Display non-system user processes.
#
# Supported:
#   RHEL 8 / RHEL 9 / RHEL 10
#

set -euo pipefail

HOST=$(hostname -s)

# Service accounts typically excluded from operational reports.
EXCLUDE_USERS='^(root|ansible|postfix|polkitd|dbus|chrony|rpc|rpcuser|nobody)$'

echo "HOSTNAME|USER|PID|COMMAND"

ps -eo user=,pid=,args= --sort=user |
awk -v host="$HOST" -v exclude="$EXCLUDE_USERS" '
$1 !~ exclude {
    user=$1
    pid=$2

    $1=""
    $2=""
    sub(/^ +/, "", $0)

    printf "%s|%s|%s|%s\n",
           host,
           user,
           pid,
           $0
}'
echo

###########################################################################
# Summary
###########################################################################

PROC_COUNT=$(ps -eo user= |
    grep -Evc "$EXCLUDE_USERS")

USER_COUNT=$(ps -eo user= |
    grep -Ev "$EXCLUDE_USERS" |
    sort -u |
    wc -l)

echo "SUMMARY"
echo "-------"
echo "PROCESSES|${PROC_COUNT}"
echo "USERS|${USER_COUNT}"
echo

###########################################################################
# Top Consumers
###########################################################################

echo "TOP_CPU"
echo "-------"

ps -eo user,pid,%cpu,%mem,args \
   --sort=-%cpu |
head -10

echo

echo "TOP_MEMORY"
echo "----------"

ps -eo user,pid,%cpu,%mem,args \
   --sort=-%mem |
head -10

echo

echo "OVERALL_STATUS|PASS"
