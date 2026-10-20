#!/usr/bin/env bash
# STAGED REPLACEMENT ONLY: install after explicit operator approval.
# Existing PM2 process/launcher is NOT modified by copying this file.
set -Eeuo pipefail
cd /home/multiservices-1m
# Never start a second independent ETHUSDT RAW recorder during migration.
if pgrep -f '[p]ython3.*research.micro_flow_v3.raw_recorder' >/dev/null; then
    echo "[V3 CONTINUOUS] ERROR: legacy raw_recorder still running; do not duplicate" >&2
    exit 66
fi
PYTHON=/usr/bin/python3
DATA_DIR=reports/micro_flow_v3/tapes/ETHUSDT
mkdir -p "$DATA_DIR" reports/micro_flow_v3/logs
FREE_KB=$(df -Pk "$DATA_DIR" | awk 'NR==2 {print $4}')
MIN_KB=$((4 * 1024 * 1024))
[[ "$FREE_KB" =~ ^[0-9]+$ ]] || exit 70
if (( FREE_KB < MIN_KB )); then
    echo "[V3 CONTINUOUS] LOW DISK free_kb=$FREE_KB" >&2
    sleep 300
    exit 75
fi
exec "$PYTHON" -u -m research.micro_flow_v3.continuous_recorder \
    --symbol ETHUSDT \
    --db-dir "$DATA_DIR" \
    --rotate-hours 6 \
    --first-trade-timeout 20 \
    --min-free-gb 4
