#!/bin/bash
# Wraps playbooks/poweroff_all.yml - the same-day, network-only power-down
# path (no iLO/IPMI check) for when there's no time to validate the
# BMC/iLO path in system_poweroff.yml. Every targeted host, physical or
# virtual, gets a plain `systemctl poweroff` and is confirmed off purely
# by no longer answering on a network port - see that playbook's own
# header comment for full detail on what this does and does not prove.
#
# This wrapper never sets the playbook's required confirmation flag
# silently: it always prints the exact host list first (--list-hosts)
# and only proceeds if you then type YES in full capitals.
set -euo pipefail

read -rp "Which inventory group or host(s) do you want to power off? (e.g. lab, or a comma-separated hostname list): " target_hosts

echo
echo "==> Hosts that will be targeted (nothing has been touched yet):"
ansible-playbook playbooks/poweroff_all.yml -e target_hosts="$target_hosts" --list-hosts
echo

read -rp "Type YES (all capitals) to power off every host listed above: " confirm
if [[ "$confirm" != "YES" ]]; then
  echo "Confirmation not given - aborting, nothing was powered off."
  exit 1
fi

ansible-playbook playbooks/poweroff_all.yml \
  -e target_hosts="$target_hosts" \
  -e poweroff_all_confirm=true
