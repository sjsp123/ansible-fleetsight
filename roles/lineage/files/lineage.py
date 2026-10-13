#!/usr/bin/env python3
"""
lineage.py - "Why is this running?" causality tracer for Linux hosts.

Inspired by pranshuparmar/witr (https://github.com/pranshuparmar/witr,
Apache-2.0): an independent, Ansible-native Python reimplementation of
the same idea - process/port/container/file causality tracing - with no
upstream Go code reused. Attribution retained per Apache-2.0 4(c).

Built to run anywhere python3 already exists (every host Ansible can
manage) with no extra binary, package, or internet access required - it
reads /proc and shells out to systemctl/docker/podman only when those are
already present.

Answers, for a process / port / open file / container:
  1. What is running?
  2. How did it start (parent process ancestry back to PID 1)?
  3. What is keeping it running (systemd unit, container, cron, SSH
     session, interactive shell)?
  4. What context does it belong to (cwd, listening sockets, warnings)?

See roles/lineage/README.md for usage examples and exit codes.
"""
import argparse
import glob
import json
import os
import pwd
import re
import subprocess
import sys
import time

CLK_TCK = 100  # sysconf(_SC_CLK_TCK) is 100 on every Linux platform we target
DELETED_SUFFIX = " (deleted)"
HIGH_RSS_KB = 1024 * 1024  # 1 GB
OLD_PROCESS_DAYS = 90
INTERACTIVE_SHELLS = {"bash", "sh", "zsh", "fish", "tmux", "tmux: server", "screen"}

EXIT_OK = 0
EXIT_WARNINGS = 1
EXIT_NOT_FOUND = 2
EXIT_PERMISSION_DENIED = 3
EXIT_INVALID_INPUT = 4
EXIT_INTERNAL_ERROR = 5


# --------------------------------------------------------------------------
# /proc readers
# --------------------------------------------------------------------------

def list_pids():
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            yield int(entry)


def read_file(path):
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except PermissionError:
        return ""


def boot_time():
    stat = read_file("/proc/stat") or ""
    for line in stat.splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    return int(time.time())


def read_stat(pid):
    """Parse /proc/<pid>/stat, handling the comm field's own parentheses."""
    raw = read_file(f"/proc/{pid}/stat")
    if not raw:
        return None
    try:
        lparen = raw.index("(")
        rparen = raw.rindex(")")
    except ValueError:
        return None
    comm = raw[lparen + 1:rparen]
    rest = raw[rparen + 2:].split()
    # fields after comm, 0-indexed from state (field 3 in `man proc`)
    fields = ["state", "ppid", "pgrp", "session", "tty_nr", "tpgid", "flags",
              "minflt", "cminflt", "majflt", "cmajflt", "utime", "stime",
              "cutime", "cstime", "priority", "nice", "num_threads",
              "itrealvalue", "starttime"]
    data = dict(zip(fields, rest))
    try:
        return {
            "pid": pid,
            "comm": comm,
            "ppid": int(data["ppid"]),
            "tty_nr": int(data["tty_nr"]),
            "starttime_ticks": int(data["starttime"]),
        }
    except (KeyError, ValueError):
        return None


def read_cmdline(pid):
    raw = read_file(f"/proc/{pid}/cmdline") or ""
    parts = [p for p in raw.split("\0") if p]
    return " ".join(parts)


def read_status_field(pid, key):
    raw = read_file(f"/proc/{pid}/status") or ""
    for line in raw.splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    return None


def read_uid(pid):
    val = read_status_field(pid, "Uid")
    if not val:
        return None
    try:
        return int(val.split()[0])
    except (ValueError, IndexError):
        return None


def username(uid):
    if uid is None:
        return "unknown"
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def read_rss_kb(pid):
    val = read_status_field(pid, "VmRSS")
    if not val:
        return 0
    try:
        return int(val.split()[0])
    except (ValueError, IndexError):
        return 0


def read_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def read_exe(pid):
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def read_environ(pid):
    raw = read_file(f"/proc/{pid}/environ")
    if raw is None:
        return None
    env = {}
    for pair in raw.split("\0"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            env[k] = v
    return env


def read_cgroup(pid):
    return read_file(f"/proc/{pid}/cgroup") or ""


def start_time_epoch(pid_stat):
    return boot_time() + pid_stat["starttime_ticks"] // CLK_TCK


def process_exists(pid):
    return os.path.isdir(f"/proc/{pid}")


# --------------------------------------------------------------------------
# Target resolution
# --------------------------------------------------------------------------

def find_by_name(name, exact):
    matches = []
    needle = name.lower()
    for pid in list_pids():
        st = read_stat(pid)
        if not st:
            continue
        comm = st["comm"]
        cmdline = read_cmdline(pid)
        haystack = (comm + " " + cmdline).lower()
        hit = (comm.lower() == needle) if exact else (needle in haystack)
        if hit:
            matches.append(pid)
    return sorted(matches)


def find_by_port(port):
    """Match a local TCP/UDP port to owning pid(s) via socket inodes."""
    port_hex = format(int(port), "04X")
    inodes = set()
    for proto_file in ("/proc/net/tcp", "/proc/net/tcp6", "/proc/net/udp", "/proc/net/udp6"):
        raw = read_file(proto_file)
        if not raw:
            continue
        for line in raw.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 10:
                continue
            local_addr = cols[1]
            try:
                local_port_hex = local_addr.split(":")[1]
            except IndexError:
                continue
            if local_port_hex.upper() == port_hex:
                inodes.add(cols[9])
    if not inodes:
        return []
    return _pids_holding_inodes(inodes)


def find_by_file(path):
    real = os.path.realpath(path)
    matches = []
    for pid in list_pids():
        try:
            if read_cwd(pid) == real:
                matches.append(pid)
                continue
            if read_exe(pid) == real:
                matches.append(pid)
                continue
            fd_dir = f"/proc/{pid}/fd"
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target == real:
                    matches.append(pid)
                    break
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return sorted(set(matches))


def _pids_holding_inodes(inodes):
    matches = []
    for pid in list_pids():
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            m = re.match(r"socket:\[(\d+)\]", target)
            if m and m.group(1) in inodes:
                matches.append(pid)
                break
    return sorted(set(matches))


def find_by_container(name):
    """Best-effort: ask docker/podman for a matching container's main PID."""
    for runtime in ("docker", "podman"):
        if _which(runtime) is None:
            continue
        try:
            out = subprocess.run(
                [runtime, "ps", "-a", "--format", "{{.ID}} {{.Names}} {{.Image}} {{.Command}}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        needle = name.lower()
        for line in out.stdout.splitlines():
            if needle in line.lower():
                cid = line.split()[0]
                pid_out = subprocess.run(
                    [runtime, "inspect", "--format", "{{.State.Pid}}", cid],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True, timeout=5,
                )
                if pid_out.returncode == 0:
                    try:
                        pid = int(pid_out.stdout.strip())
                        if pid > 0:
                            return [pid], runtime, cid
                    except ValueError:
                        pass
    return [], None, None


def _which(binary):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(d, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# --------------------------------------------------------------------------
# Ancestry / source / context
# --------------------------------------------------------------------------

def build_ancestry(pid):
    chain = []
    seen = set()
    current = pid
    while current and current not in seen:
        st = read_stat(current)
        if not st:
            break
        chain.append((current, st["comm"]))
        seen.add(current)
        if current == 1:
            break
        current = st["ppid"]
    chain.reverse()
    return chain


def build_children(pid, limit=10):
    children = {}
    for candidate in list_pids():
        st = read_stat(candidate)
        if st and st["ppid"] == pid:
            children.setdefault(pid, []).append((candidate, st["comm"]))
    return children.get(pid, [])[:limit]


def systemd_unit_for(pid):
    cgroup = read_cgroup(pid)
    for line in cgroup.splitlines():
        parts = line.split(":", 2)
        path = parts[2] if len(parts) == 3 else line
        m = re.search(r"/([^/]+\.service)$", path)
        if m:
            return m.group(1)
        m = re.search(r"/([^/]+\.scope)$", path)
        if m:
            return None  # user session scope, not a managed unit
    return None


def container_context(pid):
    cgroup = read_cgroup(pid)
    if "docker" in cgroup or "libpod" in cgroup or "kubepods" in cgroup:
        m = re.search(r"([0-9a-f]{12,64})(?:\.scope)?$", cgroup.strip().splitlines()[-1] if cgroup.strip() else "")
        cid = m.group(1)[:12] if m else None
        kind = "kubernetes pod" if "kubepods" in cgroup else ("podman" if "libpod" in cgroup else "docker")
        return kind, cid
    return None, None


def detect_source(pid, ancestry):
    """Return (label, detail) - best-effort, one primary source only."""
    unit = systemd_unit_for(pid)
    if unit:
        detail = unit
        timer_check = subprocess.run(
            ["systemctl", "list-timers", "--all", "--no-legend"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=5,
        ) if _which("systemctl") else None
        if timer_check and timer_check.returncode == 0:
            unit_base = unit.rsplit(".", 1)[0]
            for line in timer_check.stdout.splitlines():
                if f"{unit_base}.timer" in line or f"{unit_base}.service" in line:
                    detail = f"{unit} (triggered by {unit_base}.timer)"
                    break
        return "systemd unit", detail

    kind, cid = container_context(pid)
    if kind:
        return kind, cid or "unknown container id"

    comms = [c.lower() for _, c in ancestry]
    if any("crond" in c or c == "cron" for c in comms):
        return "cron", "scheduled via crond"

    if any("sshd" in c for c in comms):
        ssh_pid = next((p for p, c in ancestry if "sshd" in c.lower()), None)
        env = read_environ(ssh_pid) if ssh_pid else None
        remote = (env or {}).get("SSH_CONNECTION", "").split()
        remote_ip = remote[0] if remote else "unknown"
        return "SSH session", f"remote {remote_ip}"

    if any(c in INTERACTIVE_SHELLS for c in comms):
        my_stat = read_stat(pid)
        tty = my_stat["tty_nr"] if my_stat else 0
        if tty != 0:
            return "interactive shell", "has a controlling TTY"

    if ancestry and ancestry[0][0] == 1:
        return "systemd", "direct child of PID 1 (no matching unit found - may be a transient/scope process)"

    return "unknown", "could not determine a primary source (process may have been reparented)"


def sockets_for(pid):
    """Listening TCP sockets owned by this pid (best-effort via /proc)."""
    fd_dir = f"/proc/{pid}/fd"
    inodes = set()
    try:
        for fd in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            m = re.match(r"socket:\[(\d+)\]", target)
            if m:
                inodes.add(m.group(1))
    except (FileNotFoundError, PermissionError):
        return []

    if not inodes:
        return []

    results = []
    for proto_name, proto_file in (("TCP", "/proc/net/tcp"), ("TCP6", "/proc/net/tcp6"),
                                    ("UDP", "/proc/net/udp"), ("UDP6", "/proc/net/udp6")):
        raw = read_file(proto_file)
        if not raw:
            continue
        for line in raw.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 10 or cols[9] not in inodes:
                continue
            local_addr, local_port_hex = cols[1].split(":")
            port = int(local_port_hex, 16)
            state_hex = cols[3]
            state = "LISTENING" if state_hex == "0A" else ("ESTABLISHED" if state_hex == "01" else state_hex)
            addr = _decode_addr(local_addr, proto_name)
            results.append(f"{addr}:{port} ({proto_name.rstrip('6')} | {state})")
    return sorted(set(results))


def _decode_addr(hex_addr, proto_name):
    if proto_name in ("TCP", "UDP"):
        b = bytes.fromhex(hex_addr)
        octets = b[::-1]
        addr = ".".join(str(o) for o in octets)
        return "0.0.0.0" if addr == "0.0.0.0" else addr
    return "::" if set(hex_addr) == {"0"} else "[" + hex_addr + "]"


# --------------------------------------------------------------------------
# Warnings
# --------------------------------------------------------------------------

def collect_warnings(pid, uid, sockets, ancestry):
    warnings = []
    if uid == 0:
        warnings.append("Process is running as root")
    if any(s.startswith("0.0.0.0:") or s.startswith("::") for s in sockets):
        warnings.append("Process is listening on a public interface (0.0.0.0 / ::)")
    rss = read_rss_kb(pid)
    if rss > HIGH_RSS_KB:
        warnings.append(f"Process is using high memory ({rss / (1024 * 1024):.1f} GB RSS)")
    st = read_stat(pid)
    if st:
        age_days = (time.time() - start_time_epoch(st)) / 86400
        if age_days > OLD_PROCESS_DAYS:
            warnings.append(f"Process has been running for over {OLD_PROCESS_DAYS} days ({int(age_days)}d)")
    exe = read_exe(pid)
    if exe and exe.endswith(DELETED_SUFFIX):
        warnings.append(f"Running from a deleted binary on disk: {exe}")
    env = read_environ(pid) or {}
    if "LD_PRELOAD" in env:
        warnings.append(f"LD_PRELOAD set: {env['LD_PRELOAD']} (possible library injection)")
    return warnings


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def format_ancestry_short(ancestry):
    return " \u2192 ".join(f"{comm} (pid {pid})" for pid, comm in ancestry)


def format_ancestry_tree(ancestry, children):
    lines = []
    for depth, (pid, comm) in enumerate(ancestry):
        prefix = "  " * depth + ("\u2514\u2500 " if depth else "")
        marker = "  <== TARGET" if pid == ancestry[-1][0] else ""
        lines.append(f"{prefix}{comm} (pid {pid}){marker}")
    if children:
        depth = len(ancestry)
        for i, (cpid, ccomm) in enumerate(children):
            branch = "\u2514\u2500 " if i == len(children) - 1 else "\u251c\u2500 "
            lines.append("  " * depth + branch + f"{ccomm} (pid {cpid})")
    return "\n".join(lines)


def gather_report(pid, verbose):
    st = read_stat(pid)
    ancestry = build_ancestry(pid)
    children = build_children(pid)
    uid = read_uid(pid)
    sockets = sockets_for(pid)
    source_label, source_detail = detect_source(pid, ancestry)
    warnings = collect_warnings(pid, uid, sockets, ancestry)
    started_epoch = start_time_epoch(st) if st else None
    return {
        "pid": pid,
        "comm": st["comm"] if st else "?",
        "cmdline": read_cmdline(pid),
        "user": username(uid),
        "uid": uid,
        "started_epoch": started_epoch,
        "started_human": time.strftime("%a %Y-%m-%d %H:%M:%S %z", time.localtime(started_epoch)) if started_epoch else "unknown",
        "ancestry": ancestry,
        "children": children,
        "source_label": source_label,
        "source_detail": source_detail,
        "cwd": read_cwd(pid),
        "sockets": sockets,
        "warnings": warnings,
        "environ": read_environ(pid) if verbose else None,
    }


def render_standard(target_label, report):
    lines = []
    lines.append(f"Target      : {target_label}")
    lines.append("")
    lines.append(f"Process     : {report['comm']} (pid {report['pid']})")
    lines.append(f"User        : {report['user']}")
    lines.append(f"Command     : {report['cmdline'] or report['comm']}")
    lines.append(f"Started     : {report['started_human']}")
    lines.append("")
    lines.append("Why It Exists :")
    lines.append("  " + format_ancestry_short(report["ancestry"]))
    lines.append("")
    lines.append(f"Source      : {report['source_label']} ({report['source_detail']})")
    lines.append("")
    if report["cwd"]:
        lines.append(f"Working Dir : {report['cwd']}")
    if report["sockets"]:
        lines.append("Sockets     : " + ", ".join(report["sockets"]))
    if report["warnings"]:
        lines.append("")
        lines.append("Warnings    :")
        for w in report["warnings"]:
            lines.append(f"  - {w}")
    if report["environ"] is not None:
        lines.append("")
        lines.append("Environment :")
        for k, v in sorted(report["environ"].items()):
            lines.append(f"  {k}={v}")
    return "\n".join(lines)


def to_json(target_label, report):
    out = dict(report)
    out["target"] = target_label
    out["ancestry"] = [{"pid": p, "comm": c} for p, c in report["ancestry"]]
    out["children"] = [{"pid": p, "comm": c} for p, c in report["children"]]
    return json.dumps(out, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='lineage.py - "Why is this running?" causality trace for RHEL/Linux hosts.'
    )
    parser.add_argument("names", nargs="*", help="process/service name(s), substring-matched unless --exact")
    parser.add_argument("-p", "--pid", action="append", default=[], help="pid(s) to look up")
    parser.add_argument("-o", "--port", action="append", default=[], help="local port(s) to look up")
    parser.add_argument("-f", "--file", action="append", default=[], help="file(s) held open by a process")
    parser.add_argument("-c", "--container", action="append", default=[], help="container name/image substring")
    parser.add_argument("-x", "--exact", action="store_true", help="exact name matching (no substring search)")
    parser.add_argument("-s", "--short", action="store_true", help="show only the ancestry chain")
    parser.add_argument("-t", "--tree", action="store_true", help="show ancestry + children as a tree")
    parser.add_argument("--json", action="store_true", help="show result as JSON")
    parser.add_argument("--env", action="store_true", help="include environment variables")
    parser.add_argument("--verbose", action="store_true", help="alias for --env plus extra detail")
    parser.add_argument("--warnings", action="store_true", help="show only warnings")
    args = parser.parse_args()

    targets = []  # list of (label, [pids])
    invalid = False

    for pid_str in args.pid:
        try:
            pid = int(pid_str)
        except ValueError:
            print(f"Invalid pid: {pid_str}", file=sys.stderr)
            invalid = True
            continue
        targets.append((f"pid: {pid}", [pid] if process_exists(pid) else []))

    for port in args.port:
        targets.append((f"port: {port}", find_by_port(port)))

    for path in args.file:
        targets.append((f"file: {path}", find_by_file(path)))

    for cname in args.container:
        pids, runtime, cid = find_by_container(cname)
        label = f"container: {cname}" + (f" ({runtime} {cid})" if cid else "")
        targets.append((label, pids))

    for name in args.names:
        targets.append((f"name: {name}", find_by_name(name, args.exact)))

    if invalid:
        sys.exit(EXIT_INVALID_INPUT)
    if not targets:
        print("No target given. Pass a name, --pid, --port, --file, or --container.", file=sys.stderr)
        sys.exit(EXIT_INVALID_INPUT)

    overall_rc = EXIT_OK
    outputs = []
    multi = len(targets) > 1

    for label, pids in targets:
        if not pids:
            outputs.append(f"----- [{label}] -----\nNot found: no matching process, port, file holder, or container.")
            overall_rc = max(overall_rc, EXIT_NOT_FOUND)
            continue

        # Ambiguity only makes sense for a name-based lookup (substring/exact
        # match against comm+cmdline). A --pid is already a single exact pid;
        # --port/--file/--container returning >1 hit is a legitimate multi-
        # process situation (e.g. SO_REUSEPORT, a forked worker with the same
        # fd open), not something to prompt the user to disambiguate. This is
        # checked per-target (label), not against the whole args object, so
        # combining e.g. --port with an ambiguous name in the SAME run still
        # shows the disambiguation list for that name instead of silently
        # picking its first match just because a --port flag was also given.
        if len(pids) > 1 and label.startswith("name: "):
            lines = ["Multiple matching processes found:", ""]
            for i, pid in enumerate(pids, 1):
                lines.append(f"[{i}] {read_stat(pid)['comm'] if read_stat(pid) else '?'} (pid {pid})")
                lines.append(f"    {read_cmdline(pid)}")
            lines.append("")
            lines.append("Re-run with:")
            lines.append("  lineage.py --pid <pid>")
            outputs.append(("\n".join([f"----- [{label}] -----"] + lines)) if multi else "\n".join(lines))
            overall_rc = max(overall_rc, EXIT_INVALID_INPUT)
            continue

        pid = pids[0]
        try:
            report = gather_report(pid, verbose=(args.env or args.verbose))
        except PermissionError:
            outputs.append(f"----- [{label}] -----\nPermission denied reading /proc/{pid}. Re-run as root / with become: true.")
            overall_rc = max(overall_rc, EXIT_PERMISSION_DENIED)
            continue

        if report["warnings"]:
            overall_rc = max(overall_rc, EXIT_WARNINGS)

        if args.json:
            body = to_json(label, report)
        elif args.warnings:
            body = "\n".join(f"- {w}" for w in report["warnings"]) or "No warnings."
        elif args.tree:
            body = format_ancestry_tree(report["ancestry"], report["children"])
        elif args.short:
            body = format_ancestry_short(report["ancestry"])
        else:
            body = render_standard(label, report)

        outputs.append(f"----- [{label}] -----\n{body}" if multi else body)

    print("\n\n".join(outputs))
    sys.exit(overall_rc)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - top-level safety net, exit code 5 = internal error
        print(f"Internal error: {exc}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL_ERROR)
