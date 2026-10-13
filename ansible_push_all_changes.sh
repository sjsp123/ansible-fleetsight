#!/bin/bash
# One-command redeploy for BOTH web UIs (linuxdb_dashboard + log_search_ui) -
# run this after any change that touches both apps, e.g. the shared
# cross-app navigation switcher, so you don't have to remember to run the
# two single-app scripts separately and don't forget one of them.
#
# This just runs the two existing single-purpose scripts in sequence, so
# it always does exactly what they do (including their own git-checkout
# detection/pull - a real git pull on the home lab, skipped automatically
# on a non-git copy of ~/ansible, see
# inventory default) - nothing is duplicated here that could drift out
# of sync with them:
#   ansible_pushdb_change.sh          -> linuxdb_dashboard_ui tag
#   ansible_push_logsearch_change.sh  -> log_search_ui tag
#
# Each of those two scripts already has "set -euo pipefail", so if either
# playbook run fails, this script stops immediately (the second app is
# NOT deployed) and exits non-zero - same fail-fast behaviour as running
# them by hand one after the other.
#
# Use this script when both apps changed. If only one app changed, the
# matching single-app script is still fine on its own and slightly
# faster (one git-checkout check/pull instead of two).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Redeploying linuxdb_dashboard..."
"$SCRIPT_DIR/ansible_pushdb_change.sh"

echo "==> Redeploying log_search_ui..."
"$SCRIPT_DIR/ansible_push_logsearch_change.sh"

echo "==> Both apps redeployed."
