#!/usr/bin/env bash
# Cursor Cloud / environment-build install. Must stay idempotent and terminate.
# The personal Cloud Agent environment currently runs this path as `install`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export DEBIAN_FRONTEND=noninteractive

need_venv_pkg() {
  ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1
}

if need_venv_pkg; then
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3-venv
  else
    apt-get update -y
    apt-get install -y python3-venv
  fi
fi

if ! .venv/bin/python -c 'import pip' >/dev/null 2>&1; then
  rm -rf .venv
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check

# Cloud Agent VMs are a local/dev runtime. Production validation requires
# SECRET_KEY, PAIR_TOKEN_PEPPER, and a non-memory rate-limit store when DEBUG is
# false. Persist a gitignored .env so flask init-db and later boots can start.
if [ ! -f .env ]; then
  umask 077
  secret_key="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
  pair_token_pepper="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
  cat > .env <<EOF
DEBUG=true
SECRET_KEY=${secret_key}
PAIR_TOKEN_PEPPER=${pair_token_pepper}
DATABASE_URI=sqlite:///health_weather.db
RATE_LIMIT_STORAGE_URI=memory://
DEMO_MODE=1
QWEATHER_AUTH_MODE=disabled
EOF
fi

export FLASK_APP=app.py
.venv/bin/flask init-db
