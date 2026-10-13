# Role: idm_healthcheck

## Purpose

This role runs the Identity Management (IdM) `ipa-healthcheck` tool on IdM masters and replicas after patching and reboots, summarises the results, and optionally fails the play when ERROR/CRITICAL issues are detected. 
It can also copy the JSON output to a central share for evidence and reporting.

Red Hat and FreeIPA documentation describe `ipa-healthcheck` as the primary tool for checking IdM services, replication, and topology, with `--output-type=json` providing machine-readable results that can be consumed by automation.

## Behaviour

- Optionally ensures the `ipa-healthcheck` package is installed via `dnf`.
- Ensures the healthcheck log directory exists (default `/var/log/ipa/healthcheck`).
- Builds a healthcheck command based on `idm_hc_mode`:
  - `all` – runs all configured checks.
  - `services` – runs `ipahealthcheck.meta.services` only.
  - `replication_topology` – runs `ipahealthcheck.ds.replication` and `ipahealthcheck.ipa.topology` only.
- Executes `ipa-healthcheck --output-type=json`, parses the JSON, and produces a per-host summary of total, errors, criticals, and warnings.
- Fails the play when `idm_hc_fail_on_error` is true and any ERROR or CRITICAL items are present.
- Optionally copies the JSON output to a central share via delegation for evidence collection.
- When `idm_hc_dashboard_enabled` is true (the default), also saves this run's parsed JSON on the control node as one more timestamped run per host, so `roles/linuxdb_dashboard` can surface IdM problems on its Overview page and a per-host "IdM Health" tab - see "Dashboard feed" below.

## Variables

### defaults/main.yml

- `idm_hc_install_package` (bool, default `true`)  
  Install `ipa-healthcheck` via `dnf` if not already present.

- `idm_hc_mode` (string, default `"all"`)  
  Healthcheck scope:
  - `all`
  - `services`
  - `replication_topology`

- `idm_hc_fail_on_error` (bool, default `true`)  
  Whether to fail the play on ERROR or CRITICAL healthcheck results.

- `idm_hc_log_dir` (string, default `"/var/log/ipa/healthcheck"`)  
  Directory containing IdM healthcheck logs when the timer/service is configured.

- `idm_hc_extra_sources` (list, default `[]`)  
  Additional `--source` values for `ipa-healthcheck` if required.

- `idm_hc_copy_to_share` (bool, default `false`)  
  Copy JSON output to a central share if enabled.

- `idm_hc_share_server` (string, default `""`)  
  Host to which the JSON output is delegated for copying (e.g. NFS host).

- `idm_hc_share_dest` (string, default `""`)  
  Destination directory on the share server for storing JSON outputs.

- `idm_hc_change_ref` (string, default `""`)  
  Optional change/reference ID for naming or tagging.

- `idm_hc_dashboard_enabled` (bool, default `true`)  
  Whether to also save this run's parsed JSON for `roles/linuxdb_dashboard` to read (see "Dashboard feed" below). Independent of `idm_hc_copy_to_share` - that feature copies the raw stdout to an arbitrary share/host of your choosing for evidence purposes; this one always writes the parsed result to the control node itself, specifically for the dashboard. Both can be enabled at once with no conflict.

- `idm_hc_dashboard_dir` (string, default `"/linuxdb/idm_healthcheck"`)  
  Directory on the control node (normally admin1) where the dashboard feed is written, one subdirectory per hostname. **Duplicated** in `group_vars/all/linuxdb_dashboard.yml` - `roles/linuxdb_dashboard`'s `config.py.j2` template reads this same variable but runs in a separate play/host group, so if you change this value, change it in both places (see the comment in `defaults/main.yml`).

- `idm_hc_dashboard_keep_runs` (int, default `30`)  
  How many timestamped dashboard-feed runs to retain per host before older ones are pruned.

## Dashboard feed

Whenever this role runs with `idm_hc_dashboard_enabled: true` (the default), it writes its parsed `ipa-healthcheck` JSON to `{{ idm_hc_dashboard_dir }}/<hostname>/<timestamp>.json` on the control node, plus a `latest` pointer file - the same `<dir>/<hostname>/<timestamp>` layout `roles/linuxdb_reports` already uses, so `roles/linuxdb_dashboard` can read it with no new conventions.

`roles/linuxdb_dashboard` then:

- Folds any host with an ERROR/CRITICAL or WARNING result into the Overview page's fleet table (whole-row highlight, same as the existing disk-space check) and shows a fleet-wide "IdM Health" summary line naming the affected host(s).
- Adds an "IdM Health" tab to that host's detail page - showing the run-history dropdown, a pass/warning/critical summary count, and a table of every non-`SUCCESS` finding (source, check, result, message). The tab only appears for hosts that actually have dashboard-feed data, so it stays out of the way for the rest of the fleet.

This works unchanged whether there is exactly one IdM server (homelab) or several replicated ones (work, six servers) - the dashboard has no assumption about how many IdM hosts exist, it just reads whatever is under `idm_hc_dashboard_dir`.

See `roles/linuxdb_dashboard/README.md` for the dashboard side of this integration, and `roles/idm_healthcheck_watch`/`roles/idm_healthcheck_watcher` for the separate proactive email-alerting path (syslog + Postfix relay) that complements this dashboard view.

## Usage

### Example: post-patch IdM healthcheck playbook

```yaml
***
- name: IdM post-patch healthcheck
  hosts: idm
  gather_facts: true
  become: true
  tags:
    - idm
    - healthcheck
    - post_patch

  roles:
    - role: idm_healthcheck
      vars:
        idm_hc_mode: "replication_topology"
        idm_hc_fail_on_error: true
        idm_hc_copy_to_share: true
        idm_hc_share_server: "idm-share-host"
        idm_hc_share_dest: "/nfs/idm/healthcheck"
        idm_hc_change_ref: "{{ change_ref | default('manual') }}"

