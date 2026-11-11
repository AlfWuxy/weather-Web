#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  printf '%s\n' '拒绝以 root 运行定时任务入口，请使用专用非 root 应用账号运行' >&2
  exit 1
fi

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

exec "$VENV_PY" -m services.pipelines.dispatch_alerts "$@"
