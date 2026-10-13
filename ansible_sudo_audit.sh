#!/bin/bash
# Audits sudo access rules (/etc/sudoers and /etc/sudoers.d) across the
# whole fleet and flags root-equivalent / NOPASSWD grants for review.
# Read-only. Always runs fleet-wide (no target prompt). Writes to
# /var/tmp/sudo_audit_report.csv on this control node, restricted to
# owner-read-only (0600) since it lists privileged access per host.
ansible-playbook playbooks/sudo_audit_report.yml
