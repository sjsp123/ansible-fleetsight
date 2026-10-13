#!/bin/bash
# Reports each targeted host's most recent dnf transaction (install/upgrade/
# removed package counts and when it ran) to a consolidated CSV. Read-only.
# Prompts for the target host/group (defaults to "all" if you just press
# Enter). Writes to /tmp/dnf_history_report.csv on this control node.
ansible-playbook playbooks/dnf_history.yml
