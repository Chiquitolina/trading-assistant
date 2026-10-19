
#!/usr/bin/env bash
# Micro Flow V3 — research-only ETHUSDT recorder supervisor entrypoint.
# PM2 restarts this script; each start creates a distinct SQLite tape.
set -Eeuo pipefail

cd /home/multiservices-1m

PYTHON=/usr/bin/python3
DATA_DIR=reports/micro_flow_v3/tapes/ETHUSDT
mkdir -p "$DATA_DIR" reports/micro_flow_v3/logs

# Back off on low disk rather than creating a new failed SQLite every 10 seconds.
FREE_KB=$(df -Pk "$DATA_DIR" | awk 'NR==2 {print $4}')
MIN_KB=$((4 * 1024 * 1024))
if [[ ! "$FREE_KB" =~ ^[0-9]+$ ]]; then
    echo '[V3 SERVICE] ERROR: Cannot determine free disk space' >&2
    exit 70
fi
if (( FREE_KB < MIN_KB )); then
    echo "[V3 SERVICE] LOW DISK: free_kb=$FREE_KB required_kb=$MIN_KB; no new tape created" >&2
    sleep 300
    exit 75
fi

STAMP=$(date -u +'%Y%m%dT%H%M%S_%N')
DB="$DATA_DIR/aggtrades_raw_${STAMP}.sqlite3"
if [[ -e "$DB" ]]; then
    echo "[V3 SERVICE] ERROR: tape path already exists: $DB" >&2
    exit 73
fi

echo "[V3 SERVICE] UTC start=$STAMP symbol=ETHUSDT max_hours=6"
echo "[V3 SERVICE] db=$DB"

# `exec`: SIGINT/SIGTERM sent by PM2 reaches the recorder for SQLite close.
exec "$PYTHON" -u -m research.micro_flow_v3.raw_recorder \
    --symbol ETHUSDT \
    --db "$DB" \
    --max-hours 6 \
    --first-trade-timeout 20 \
    --min-free-gb 4
