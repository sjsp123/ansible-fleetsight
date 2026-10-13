#!/bin/bash
# Pulls the latest ansible_operations changes and (re)deploys just the
# log_search_ui web UI (Search + Insights pages) - run this any time
# app.py, config.py.j2, or the systemd unit template changes (e.g. after
# the "Guide" header link was added), without re-running the heavier
# log_db/rsyslog_collector/log_alerter roles that the plain
# centralized_logging.yml playbook also runs.
#
# No -i flag here on purpose: ansible.cfg already sets
# "inventory = ./inventory" as the default, and the real inventory file
# on this host is literally named "inventory" (not "inventory/hosts.ini")
# - same reasoning as ansible_pushdb_change.sh, which this script mirrors.
#
# The git-pull below runs only when this checkout is a real git repo;
# any non-git copy of this directory (e.g. a plain tar/ZIP copy onto a
# host without GitHub access) skips straight to the playbook run, on
# the assumption the latest files were copied into place first.

set -euo pipefail

cd "$HOME"/ansible

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Git checkout detected - pulling latest changes..."
    git pull
else
    echo "No git repository at $HOME/ansible - this looks like the"
    echo "non-git checkout - skipping git pull."
    echo "Skipping git pull. Make sure you already copied the latest"
    echo "latest files into place before running this script."
fi

/usr/bin/ansible-playbook playbooks/centralized_logging.yml --tags log_search_ui
