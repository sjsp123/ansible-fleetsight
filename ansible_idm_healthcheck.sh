#!/bin/bash
# Runs ipa-healthcheck against the target IdM server(s) and reports the
# result - the same check behind the IdM Health tab in LinuxDB, but for an
# on-demand run (e.g. right after patching or rebooting an IdM host). Read-
# only. Prompts for the target host/group - type the actual IdM host or
# group name here rather than accepting the "all" default, since running
# ipa-healthcheck fleet-wide against non-IdM hosts isn't useful.
ansible-playbook playbooks/idm_healthcheck.yml
