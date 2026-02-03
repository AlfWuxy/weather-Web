#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -z "${VENV_PY:-}" ]; then
  for CANDIDATE in "$ROOT_DIR/.venv2/bin/python" "$ROOT_DIR/venv/bin/python" "$ROOT_DIR/.venv/bin/python"; do
    if [ -x "$CANDIDATE" ]; then
      VENV_PY="$CANDIDATE"
      break
    fi
  done
fi

VENV_PY="${VENV_PY:-python}"

TARGET_DATE="${1:-}"

ARGS=("--daily" "--action-daily")
if [ -n "$TARGET_DATE" ]; then
  ARGS+=("--date" "$TARGET_DATE")
fi

exec "$VENV_PY" "$ROOT_DIR/services/pipelines/sync_weather_data.py" "${ARGS[@]}"
