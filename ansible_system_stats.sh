#!/bin/bash
ansible-playbook playbooks/push_script_and_run.yml -e push_script_src=~/ansible/roles/linuxdb_reports/files/system_stats.sh -e push_script_dest=/var/tmp -e push_script_run=true -e push_script_cleanup=true

