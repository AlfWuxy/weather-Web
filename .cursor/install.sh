#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for the Flask weather/health web app.
# Prepares a Python virtualenv, installs pinned dependencies, and initializes
# the local SQLite database so the dev server and pytest suite are ready.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Stock Cloud Agent images often ship Python without ensurepip. Install the
# venv seed package first; a failed python3 -m venv can leave a broken .venv
# that still has bin/python but no pip.
if ! python3 -c "import ensurepip" 2>/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv python3.12-venv
fi

if [ ! -x .venv/bin/pip ]; then
  rm -rf .venv
  python3 -m venv .venv
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Initialize the database schema (creates tables on an empty DB, otherwise
# applies Alembic migrations). DEBUG=true skips production config validation.
DEBUG=true FLASK_APP=app.py .venv/bin/flask init-db
