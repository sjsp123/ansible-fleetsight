#!/bin/bash
# Collects HPE iLO/BIOS/firmware and storage-controller inventory from every
# host in the hpe_ilo_hosts inventory group. Read-only - no confirmation
# needed. Writes a consolidated CSV to /var/tmp/hpe_firmware_inventory.csv
# on this control node.
ansible-playbook playbooks/collect_ilo.yml
