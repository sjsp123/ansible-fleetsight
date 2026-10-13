#!/usr/bin/env bash
# Read-only storage measurements on the PoC collector; no deployment actions.
set -uo pipefail

if [[ $# -gt 0 ]]; then
    if [[ $# -eq 1 && ( "$1" == "--help" || "$1" == "-h" ) ]]; then
        printf '%s\n' \
            "Usage: bash scripts/collect_storage_sizing.sh" \
            "Run on the collector hosting /var/log/remote, /var/lib/pgsql and /linuxdb." \
            "Requires sudo access and the project defaults: database syslog, table public.systemevents." \
            "Prints measurements to stdout; errors go to stderr. No report file is created." \
            "Database queries are read-only, with 60-second statement and 3-second lock timeouts." \
            "Exit status: 0 all checks passed; 1 incomplete measurements; 2 invalid arguments."
        exit 0
    fi
    echo "Unexpected arguments. Use --help for usage." >&2
    exit 2
fi

failures=0
run_check() {
    local title=$1
    shift
    printf '\n=== %s ===\n' "$title"
    if "$@"; then
        return 0
    else
        local result=$?
        printf 'WARNING: %s failed (exit %s); retaining other measurements.\n' \
            "$title" "$result" >&2
        failures=$((failures + 1))
        return 0
    fi
}

host_directory_count() {
    sudo find /var/log/remote \
        -mindepth 1 -maxdepth 1 -type d -printf '.\n' | wc -l
}

# Avoid a postgres working-directory permission warning from private home paths.
cd /tmp || exit 1

run_check "Measurement time" date -Is
run_check "Filesystem capacity and usage, bytes" \
    df -B1 /var/log/remote /var/lib/pgsql /linuxdb
run_check "Volume-group capacity and free space, GiB" \
    sudo vgs --readonly --units g -o vg_name,vg_size,vg_free
run_check "Actual directory usage, bytes" \
    sudo du -sx -B1 /var/log/remote /var/lib/pgsql /linuxdb
run_check "Number of collected-host directories" host_directory_count

run_check "PostgreSQL measurements" sudo -u postgres env \
    PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=60000 -c lock_timeout=3000' \
    psql -X -v ON_ERROR_STOP=1 -P pager=off -d syslog <<'SQL'
SHOW transaction_read_only;

SELECT
    pg_database_size(current_database()) AS database_bytes,
    pg_size_pretty(pg_database_size(current_database())) AS database_size;

SELECT
    pg_table_size('public.systemevents') AS table_and_toast_bytes,
    pg_indexes_size('public.systemevents') AS indexes_bytes,
    pg_total_relation_size('public.systemevents') AS total_relation_bytes,
    pg_size_pretty(
        pg_total_relation_size('public.systemevents')
    ) AS total_relation_size;

SELECT
    count(*) AS retained_messages,
    count(DISTINCT fromhost) AS distinct_host_labels,
    min(receivedat) AS oldest_received,
    max(receivedat) AS newest_received,
    round(
        extract(epoch FROM (max(receivedat) - min(receivedat)))
        / 86400.0, 2
    ) AS retained_span_days
FROM public.systemevents;

SELECT
    receivedat::date AS log_date,
    count(*) AS messages,
    count(DISTINCT fromhost) AS distinct_host_labels
FROM public.systemevents
WHERE receivedat >= current_date - interval '7 days'
  AND receivedat < current_date
GROUP BY receivedat::date
ORDER BY log_date;
SQL

printf '\n=== Collection status ===\n'
if [[ $failures -ne 0 ]]; then
    printf 'INCOMPLETE: %s check(s) failed. Keep the partial output and errors.\n' \
        "$failures" >&2
    exit 1
fi
echo "All checks completed. Results are measurements, not an automatic sizing decision."
