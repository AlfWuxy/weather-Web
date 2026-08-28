#!/usr/bin/env bash
# Cloud Agent install: create .venv, install Python deps, bootstrap SQLite.
# Idempotent. Must terminate; do not start the Flask server here.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
FLASK_APP=app.py .venv/bin/flask init-db
