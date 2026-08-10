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

# 锁路径只由应用正式配置读取，wrapper 不再生成临时替代路径。
exec "$VENV_PY" -m services.pipelines.dispatch_alerts "$@"
