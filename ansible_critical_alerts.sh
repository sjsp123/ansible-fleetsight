#!/bin/bash
# Scans the last 30 days of logs on each targeted host for critical/kernel
# issues, failed services, authentication failures, disk/filesystem errors,
# and recent reboots. Read-only. Prompts for the target host/group (defaults
# to "all" if you just press Enter). Writes a per-host report plus one
# consolidated report to /var/tmp/server-findings/ on this control node.
ansible-playbook playbooks/gather_critical_alerts.yml
