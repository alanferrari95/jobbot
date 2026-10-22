#!/usr/bin/env sh
# Runs `jobbot daily` (cron, twice a day: see docs/scheduling.md).
# Uses $JOBBOT_PYTHON if set, else python3. Log: logs/daily.log
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
"${JOBBOT_PYTHON:-python3}" -m jobbot daily >> logs/daily.log 2>&1
