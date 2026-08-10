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

cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"

cleanup_status=0
"$VENV_PY" -m services.pipelines.cleanup_usage_events "$@" || cleanup_status=$?

if [ "$cleanup_status" -ne 0 ]; then
  exit "$cleanup_status"
fi
exit 0
