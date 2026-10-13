#!/bin/bash
# Reports uptime, load average, network totals, and the status of key
# services (sshd, firewalld, multipathd) for every host in the fleet.
# Read-only. Always runs fleet-wide (no target prompt). Prints each host's
# report to the console and also saves a copy per host under
# playbooks/reports/ on this control node.
ansible-playbook playbooks/system_health_report.yml
