#!/bin/bash
# Pulls the latest ansible_operations changes and (re)deploys just the
# linuxdb_dashboard web UI - run this any time app.py, config.py.j2, or
# the systemd unit template changes (e.g. after a dashboard UI tweak),
# without re-running the heavier linuxdb_reports/config_snapshot
# diagnostic-collection roles that the plain playbook also runs.
#
# No -i flag here on purpose: ansible.cfg already sets
# "inventory = ./inventory" as the default, and the real inventory file
# on this host is literally named "inventory" (not "inventory/hosts.ini",
# an .ini file that doesn't exist here) - passing -i inventory/hosts.ini
# is what broke the plain command run on 2026-09-12. Every other
# ansible_*.sh wrapper in this repo already relies on that same
# ansible.cfg default, so this one now matches them.
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
    echo "ZIP-deployed work PoC checkout (see WORK_ZIP_DEPLOY.md)."
    echo "Skipping git pull. Make sure you already copied the latest"
    echo "work-branch ZIP into place before running this script."
fi

/usr/bin/ansible-playbook playbooks/linuxdb_dashboard.yml --tags linuxdb_dashboard_ui
