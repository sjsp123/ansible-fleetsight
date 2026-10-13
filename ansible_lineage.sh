#!/bin/bash
# "Why is this running?" - traces a process, port, open file, or container
# on a target host back to the systemd unit, cron job, SSH session, or
# shell responsible for it. Read-only. Fully interactive: prompts for the
# target host, then which of process/PID/port/file/container to check
# (press Enter to skip any that don't apply) and the output mode.
ansible-playbook playbooks/lineage.yml
