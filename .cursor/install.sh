#!/usr/bin/env bash
# Cloud Agent install: idempotent Python 3.12 venv + pinned requirements.
# The dashboard environment currently invokes this path as the install command.
set -euo pipefail

cd "$(dirname "$0")/.."

export DEBIAN_FRONTEND=noninteractive

sudo apt-get update -y
sudo apt-get install -y python3.12-venv

if [[ ! -x .venv/bin/python ]]; then
  rm -rf .venv
fi
python3 -m venv .venv

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check

# Cloud Agent start uses DEBUG=true. Production validation requires SECRET_KEY
# when DEBUG is unset, so bootstrap the local SQLite schema in debug mode.
DEBUG=true FLASK_APP=app.py .venv/bin/flask init-db
