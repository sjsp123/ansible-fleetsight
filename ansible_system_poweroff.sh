#!/bin/bash
# Wraps playbooks/system_poweroff.yml - the hardware-confirmed power-down
# path: iLO/IPMI-verified for hosts in the hpe_ilo_hosts inventory group,
# network-reachability-verified for everything else. See that playbook's
# own header comment and roles/system_poweroff/README.md for the full
# design and safety gates.
#
# This wrapper never sets the playbook's required confirmation flag
# silently: it always prints the exact host list first (--list-hosts)
# and only proceeds if you then type YES in full capitals.
set -euo pipefail

read -rp "Which inventory group or host(s) do you want to power off? (e.g. hpe_ilo_hosts, or a comma-separated hostname list): " target_hosts

echo
echo "==> Hosts that will be targeted (nothing has been touched yet):"
ansible-playbook playbooks/system_poweroff.yml -e target_hosts="$target_hosts" --list-hosts
echo

read -rp "Type YES (all capitals) to power off every host listed above: " confirm
if [[ "$confirm" != "YES" ]]; then
  echo "Confirmation not given - aborting, nothing was powered off."
  exit 1
fi

ansible-playbook playbooks/system_poweroff.yml \
  -e target_hosts="$target_hosts" \
  -e system_poweroff_confirm=true
