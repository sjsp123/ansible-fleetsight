#!/bin/bash
cd "$HOME"/ansible && /usr/bin/ansible-playbook playbooks/hostinfo.yml -e target_host=all
cd "$HOME"/ansible && /usr/bin/ansible-playbook playbooks/linuxdb_dashboard.yml
