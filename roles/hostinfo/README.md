ansible/
├── group_vars/
│   └── all/
│       └── host_checks.yml      # <- add new columns here
├── playbooks/
│   └── hostinfo.yml
└── roles/
    └── hostinfo/
        ├── defaults/
        │   └── main.yml
        ├── tasks/
        │   ├── main.yml
        │   └── report.yml
        └── templates/
            └── hostinfo.csv.j2



What's in each file
•	group_vars/all/host_checks.yml — rsm222_hosts plus the full host_checks registry (realm, last_reboot, uptime, time_sync, chrony_leap, chronyd_status, sshd_status, firewalld_status, auditd_status, crond_status, chrony_servers, rsm222, failed_services), with two commented-out example entries showing the pattern for a new service check and a threshold check.
•	roles/hostinfo/defaults/main.yml — host_report_csv_path, an empty host_checks: [] fallback (so the role doesn't blow up if group_vars is ever missing), and host_report_keep_runs (default 30) controlling how many timestamped history snapshots are retained.
•	roles/hostinfo/tasks/main.yml — the generic loop that runs every check, collects results into a dict, and merges them with core identity facts.
•	roles/hostinfo/tasks/report.yml — writes the live hostinfo.csv, then also saves a timestamped copy to history/hostinfo_<YYYYMMDD_HHMMSS>.csv and prunes that history/ directory down to the newest host_report_keep_runs snapshots. See "History snapshots" below.
•	playbooks/hostinfo.yml — unchanged from before.
•	roles/hostinfo/templates/hostinfo.csv.j2 — the fixed, join-based template (header and every row built as a single expression, so there's no block tag at end-of-line for trim_blocks to eat a newline after).
Drop this ansible/ folder's contents over your existing tree (or diff it in) and you're set — going forward, adding a column means editing only group_vars/all/host_checks.yml.

History snapshots
•	Every run of playbooks/hostinfo.yml writes the live report to {{ host_report_csv_path }}/hostinfo.csv as before, and now also copies that same file to {{ host_report_csv_path }}/history/hostinfo_<timestamp>.csv, where <timestamp> is the run's date +%Y%m%d_%H%M%S value (report_timestamp).
•	After saving the snapshot, the role prunes history/ down to the most recent host_report_keep_runs files (default 30) — the same ls | sort | head -n -N | xargs rm pattern used by roles/linuxdb_reports' own pruning task, just simplified for hostinfo's single-file-per-run layout.
•	roles/linuxdb_dashboard's Overview page reads this history/ directory directly to show a "last updated" timestamp for the live file and a dropdown to view any archived snapshot — no dashboard-side configuration is needed beyond keeping host_report_csv_path and linuxdb_dashboard_hostinfo_csv_path pointed at the same hostinfo.csv, as they already are by convention.

