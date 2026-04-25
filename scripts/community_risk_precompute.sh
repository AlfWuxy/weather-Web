#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -z "${VENV_PY:-}" ]; then
  for CANDIDATE in "${DEPLOY_VENV_DIR:+$DEPLOY_VENV_DIR/bin/python}" "$ROOT_DIR/.venv2/bin/python" "$ROOT_DIR/venv/bin/python" "$ROOT_DIR/.venv/bin/python"; do
    [ -n "$CANDIDATE" ] || continue
    if [ -x "$CANDIDATE" ] && \
       "$CANDIDATE" -V >/dev/null 2>&1 && \
       "$CANDIDATE" -c "import dotenv" >/dev/null 2>&1; then
      VENV_PY="$CANDIDATE"
      break
    fi
  done
fi

VENV_PY="${VENV_PY:-python3}"

cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"

exec "$VENV_PY" -m services.pipelines.precompute_community_risk "$@"
