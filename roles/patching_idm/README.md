# patching_idm — IdM patching with post-reboot service verification

Ported from a previous enterprise estate build (kept local-only,
not referenced here by name) on 2026-10-21. The estate content was written for a
different topology (a central control node with vault-held
credentials, temporary SSH keys copied to the control node); this port
keeps the patching/verification logic and drops everything that the
lab solves differently.

## What the role does

1. **Refresh metadata + list pending updates** — `dnf check-update
   --refresh` (rc 0 = updates listed, 100 = none, tolerated)
2. **Update all packages** — `dnf name='*' state=latest`
3. **Reboot if `needs-restarting -r` says so** — long tolerances for
   IdM masters (reboot timeout 3600s, 60s settle, 5-minute pause)
4. **Verify IdM health** — `ipa.service` running, then `ipactl status`
   checked keyword-by-keyword against the required service list
   (Directory, krb5kdc, kadmin, httpd, ipa-custodia, pki-tomcatd,
   ipa-otpd, named) — fails the play if any service is not RUNNING
5. **Patch history** — appends
   `hostname,timestamp,pending_count,rebooted` to
   `/linuxdb/patching/ansible_patching_history.csv` on the collector
   (log_collector delegation pattern)

## What was stripped from the original (and why)

| Original | Why dropped |
|---|---|
| `vars_files: ../secret_vars/vault.yml` + temp SSH key pre/post tasks | The estate's "control node has no direct key" constraint; the lab authenticates via svc_ansible + IPA + controller machine credential |
| `add_host`/`new_host` targeting | All lab hosts are in the inventory; targeting is survey-driven |
| `delegate_to` the estate control node + vaulted creds for history writes | Estate control node; replaced by the log_collector delegation pattern |
| `yum` | RHEL 10 / dnf |

## Variables

See `defaults/main.yml` — all knobs (reboot tolerances, required
service keywords, history path) are defaults and overridable via
extra-vars, e.g. `-e patching_idm_history_enabled=false`.

## Notes

- `max_fail_percentage: 0` and `serial: 1` in the playbook: with a
  single IdM server there is no replica to fail over to, so any failure
  stops everything — deliberate.
- The required-service list is keywords, not full lines, because
  `ipactl status` output format shifts slightly across IPA versions.
  `named` is required here because idm1 runs integrated DNS.
- Future integration: the `idm_healthchecks` role can be appended after
  a patch+reboot for a full `ipa-healthcheck` sweep — consider wiring
  that into the job template when EDA arrives.
