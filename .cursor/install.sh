#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for the Flask weather/health web app.
# Prepares a Python virtualenv, installs pinned dependencies, and initializes
# the local SQLite database so the dev server and pytest suite are ready.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Create the virtualenv. The stock image may lack the venv seeding package, so
# install python3-venv on demand before retrying.
if [ ! -x .venv/bin/python ]; then
  if ! python3 -m venv .venv 2>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-venv
    python3 -m venv .venv
  fi
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Initialize the database schema (creates tables on an empty DB, otherwise
# applies Alembic migrations). DEBUG=true skips production config validation.
DEBUG=true FLASK_APP=app.py .venv/bin/flask init-db
