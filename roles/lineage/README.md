# lineage

"Why is this running?" - traces a process, port, open file, or container
back through its parent chain to whatever is actually responsible for it -
a systemd unit, a container runtime, cron, an SSH session, or an
interactive shell - and prints a plain-English narrative explanation
straight into your `ansible-playbook` output.

No binary to download, no package to install, no internet access needed on
the target: the whole tool is one stdlib-only Python 3 script
(`files/lineage.py`) that reads `/proc` directly and shells out to
`systemctl`/`docker`/`podman` only when they're already present. Every RHEL
host Ansible manages already has the Python 3 interpreter Ansible itself
needs, so there's nothing extra to provision.

## Attribution

This tool is an independent, Ansible-native reimplementation of the idea
behind [pranshuparmar/witr](https://github.com/pranshuparmar/witr)
("Why is this running?" - process/port/container/file causality tracing,
Apache-2.0). No upstream Go code is reused: this is a from-scratch Python
implementation reading `/proc` directly, so it runs on any host Ansible
manages without installing a binary. The exit-code scheme (0 = found,
1 = found with warnings, 2 = not found, 3 = permission denied,
4 = invalid input, 5 = internal error) follows witr's contract as a
deliberate homage. Full upstream license:
https://github.com/pranshuparmar/witr/blob/main/LICENSE

## Quick start

### Interactive (no `-e` flags to remember)

Just run it with no arguments and answer the prompts - it walks you
through target host, what to look up (name/pid/port/file/container),
output mode, and the exact/verbose toggles one at a time. Press Enter to
skip any field that doesn't apply and to accept every other default:

```bash
ansible-playbook playbooks/lineage.yml
```

### Non-interactive (`-e` flags - the only reliable form for cron/AAP)

Any `-e` var you supply skips its matching prompt automatically, so this
works unattended with no stdin required:

```bash
# By process/service name (substring-matched by default)
ansible-playbook playbooks/lineage.yml -e target_host=admin1 -e target=nginx

# By port
ansible-playbook playbooks/lineage.yml -e target_host=admin1 -e target_port=8081

# By pid
ansible-playbook playbooks/lineage.yml -e target_host=admin1 -e target_pid=14233

# By an open file (e.g. what's holding a lock file)
ansible-playbook playbooks/lineage.yml -e target_host=admin1 -e target_file=/var/lib/dpkg/lock

# By container (name/image substring - checks docker, then podman)
ansible-playbook playbooks/lineage.yml -e target_host=admin1 -e target_container=redis

# Multiple names and/or multiple ports in ONE run - comma-separated
ansible-playbook playbooks/lineage.yml -e target_host=admin1 \
  -e target=nginx,postgres -e target_port=8081,5432
```

Always pass `-e target_host=<host or group>` explicitly for unattended/AAP
runs - every `vars_prompt` default in the playbook (`target_host` included)
is only a convenience for an interactive terminal, same convention as
`playbooks/hostinfo.yml`.

## Output modes

Add `-e mode=<mode>`:

| `mode`       | Shows                                                          |
| ------------ | --------------------------------------------------------------- |
| `standard`   | (default) full narrative: process, ancestry, source, context, warnings |
| `short`      | just the one-line ancestry chain (`systemd → pm2 → node`)      |
| `tree`       | ancestry + up to 10 child processes, as an indented tree       |
| `json`       | the full structured result, for piping into other tooling      |
| `env`        | standard output plus the process's environment variables       |
| `warnings`   | only the warnings list (or "No warnings.")                     |

Add `-e verbose=true` to include environment variables in any mode. Add
`-e exact=true` to require an exact name match instead of substring
matching (useful when a name query matches more than one process).

## Multiple targets in one run

Any combination of `target`, `target_pid`, `target_port`, `target_file`,
`target_container` can be set at once, and **each of those accepts a
comma-separated list** to check several values of the same type in one
go - the role fans each comma-separated `-e` value out into the repeated
flags the script expects (e.g. `target_port=8081,5432` becomes
`--port 8081 --port 5432`), so it all runs through one
`ansible-playbook` command with no shell access to the target needed:

```bash
# Three process names and two ports, all in one run
ansible-playbook playbooks/lineage.yml -e target_host=admin1 \
  -e target=nginx,postgres,redis-server -e target_port=8081,5432 -e mode=tree
```

The script queries each resolved target in turn and labels each section
(`----- [name: nginx] -----`, `----- [port: 5432] -----`, ...). The
overall exit code is the highest-severity code across all targets (e.g.
one target found + one target ambiguous + one warned → the run reports
exit 4, and each section shows which target it came from).

A name that matches more than one process shows the same disambiguation
list regardless of what else was passed alongside it (a `target_port` in
the same run no longer suppresses that listing - fixed alongside adding
multi-value support, since combining an ambiguous name with an unrelated
port in one run is now the normal case, not an edge case).

`target_file` is comma-split the same way for consistency. If a single
path genuinely contains a literal comma (rare, but valid on Linux), pass
it as the only `target_file` value in that run, or run the script
directly on the box using the copy this role already deployed:

```bash
ssh admin1 sudo /usr/local/sbin/why-running --file '/path/with,a,comma' --tree
```

## Exit codes

Surfaced by the role as a plain-English `ansible.builtin.debug` message
whenever the result is non-zero:

| Code | Meaning                                                              |
| ---- | --------------------------------------------------------------------- |
| 0    | Clean: target found, no warnings                                     |
| 1    | Warnings: target found but flagged one or more warnings              |
| 2    | Not found: no matching process, port, file holder, or container      |
| 3    | Permission denied (shouldn't occur - this role always runs `become: true`) |
| 4    | Invalid input, or an ambiguous name match - re-run with `target_pid=<pid>` |
| 5    | Internal error - the role fails the play and prints stderr           |

Only exit code 5 fails the Ansible play; the others are meaningful
diagnostic results, not automation failures, so the play still reports
`ok` and you read the answer straight out of the task output.

## What it detects as the "Source"

Best-effort, in priority order:

1. A systemd unit (parses `/proc/<pid>/cgroup`, cross-checked against
   `systemctl list-timers` to note if it's timer-triggered)
2. A container (Docker, Podman, or Kubernetes, via cgroup path - `docker`/
   `podman inspect` fills in the container name when that runtime is
   installed on the box)
3. Cron (an ancestor is `crond`)
4. An SSH session (an ancestor is `sshd` - reports the remote IP from
   `SSH_CONNECTION`)
5. An interactive shell (a `bash`/`tmux`/`screen` ancestor with a
   controlling TTY)
6. Falls back to "direct child of PID 1" when none of the above match -
   this is the honest answer for a transient/scope process, not a bug.

## Warnings it flags

- Running as root
- Listening on a public interface (`0.0.0.0` / `::`)
- High memory usage (>1 GB RSS)
- Running for more than 90 days
- Executing from a deleted binary on disk (a common sign a process needs
  restarting after a package upgrade, or - rarely - a compromise indicator)
- `LD_PRELOAD` set in its environment

## Notes

- Read-only: never sends a signal, kills a process, or restarts a unit. It
  only reads `/proc`, and optionally calls `systemctl list-timers`, `docker
  ps`/`inspect`, or `podman ps`/`inspect` (also read-only).
- `become: true` is required for full fidelity - without root, `/proc/<pid>/fd`
  for a process owned by another user can't be read, so sockets and open-file
  matches for that pid come back empty rather than the task failing.
- Safe to run on `main` (home lab), `work` (PoC), or `aap` - it makes no
  configuration changes, so there's nothing to validate before promoting it
  between branches.
