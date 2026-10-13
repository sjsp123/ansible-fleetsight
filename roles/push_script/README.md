# Role: push_script

## Purpose

Generic, ad hoc "push a script to a host, run it, show the output"
utility. It doesn't know or care what the script does — you point it at
any local file via `push_script_src` and it copies, runs, and (if asked)
cleans it up on the target(s).

This is the mechanism behind `playbooks/push_script_and_run.yml` and the
root-level `ansible_df_check.sh` / `ansible_proc_check.sh` /
`ansible_svr_check.sh` / `ansible_system_stats.sh` / `ansible_hostinfo.sh`
convenience wrappers — one-liner shortcuts for running a one-off check
against a host or group without going through a dedicated role/playbook.
As of this repo's current state, the four `linuxdb_reports` check
scripts (`df_check.sh`, `proc_check.sh`, `svr_check.sh`,
`system_stats.sh`) are pushed straight from
`roles/linuxdb_reports/files/` — there's no separate ad hoc copy to keep
in sync (see `roles/linuxdb_reports/README.md`).

Nothing is installed on the target — the script is copied to a scratch
path, optionally run, and optionally removed again afterward. Whatever
the pushed script itself does (or doesn't do) is between that script and
the target host; this role has no opinion on package installs, and
none of the scripts it currently pushes install anything either.

## Behaviour

1. Fails fast with a clear message if `push_script_src` wasn't provided.
2. Copies `push_script_src` to
   `{{ push_script_dest }}/{{ push_script_src | basename }}` on the
   target, with mode `push_script_mode`.
3. If `push_script_run` is true, runs it (`push_script_command` if set,
   otherwise the copied path itself) and shows both the captured
   stdout and the return code via `debug`.
4. If `push_script_cleanup` is true, deletes the copied script from the
   target afterward.

A non-zero exit code above `1` is treated as an Ansible task failure
(`failed_when: push_script_result.rc > 1`); exit code `1` is allowed
through, matching the pipe-delimited check scripts' convention of using
`1` to mean "ran fine, but flagged a WARNING/CRITICAL result" rather
than "the script itself broke."

## Requirements

None beyond what the pushed script itself needs already present on the
target — this role doesn't install anything.

## Usage

```bash
ansible-playbook playbooks/push_script_and_run.yml \
  -e push_script_src=~/ansible/roles/linuxdb_reports/files/svr_check.sh \
  -e push_script_dest=/var/tmp \
  -e push_script_run=true \
  -e push_script_cleanup=true
```

`playbooks/push_script_and_run.yml` prompts interactively for the
target host/group (`target_host`) if you don't pass `-e target_host=...`.
See the root `ansible_*.sh` wrapper files for ready-made one-liners
covering the four `linuxdb_reports` scripts plus `hostinfo.yml`.

## Variables

- `push_script_src` (string, **required**, no default) — local path to
  the script to push. Fails the play immediately if empty.
- `push_script_dest` (string, default `/var/tmp`) — directory on the
  target to copy the script into.
- `push_script_mode` (string, default `"0755"`) — file mode applied to
  the copied script.
- `push_script_run` (bool, default `true`) — whether to actually
  execute the script after copying it, or just deliver it and stop.
- `push_script_command` (string, default `""`, meaning "run the copied
  script directly") — override to run something else instead, e.g. a
  wrapper command or the script with extra arguments.
- `push_script_cleanup` (bool, default `false`) — whether to delete the
  copied script from the target after running it. Leave `false` if you
  want to leave a copy behind for manual re-runs.
