#!/bin/bash
# Wrapper matching the style of ./ansible_dnf_update, but running the new
# idempotent playbooks/update_system.yml instead of pushing update_system.py.
set -euo pipefail

read -rp "Enter the target host or inventory group: " target_hosts
read -rp "Reboot automatically if required? [Y/n]: " do_reboot
do_reboot=${do_reboot:-Y}

reboot_flag=true
if [[ "$do_reboot" =~ ^[Nn] ]]; then
  reboot_flag=false
fi

ansible-playbook playbooks/update_system.yml \
  -e target_hosts="$target_hosts" \
  -e system_update_reboot_enabled="$reboot_flag"
