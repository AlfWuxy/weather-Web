#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for the Flask weather/health web app.
# Creates a Python virtualenv, installs pinned dependencies, and initializes
# the local SQLite database so the agent, pytest, and the Flask app are ready.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The stock Cloud Agent image may lack the Debian venv seeding package.
# Recreate a broken leftover .venv (ensurepip failure leaves a non-executable tree).
if [ ! -x .venv/bin/python ]; then
  rm -rf .venv
  if ! python3 -m venv .venv; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
    python3 -m venv .venv
  fi
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# DEBUG=true skips production-only config validation on this local SQLite setup.
DEBUG=true FLASK_APP=app.py .venv/bin/flask init-db
