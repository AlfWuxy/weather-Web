#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# Prefer an explicit python from caller (deploy script sets VENV_PY).
VENV_PY="${VENV_PY:-}"
if [ -z "$VENV_PY" ]; then
  for CANDIDATE in "$ROOT_DIR/.venv2/bin/python" "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/venv/bin/python" python3 python; do
    if [ -x "$CANDIDATE" ]; then
      VENV_PY="$CANDIDATE"
      break
    fi
    if command -v "$CANDIDATE" >/dev/null 2>&1; then
      VENV_PY="$(command -v "$CANDIDATE")"
      break
    fi
  done
fi

if [ -z "$VENV_PY" ]; then
  echo "ERROR: cannot find python interpreter (set VENV_PY)." >&2
  exit 2
fi

echo "[server_migrate] using python: $VENV_PY"

# Ensure instance dir exists (sqlite DB lives here by default).
mkdir -p "$ROOT_DIR/instance"

echo "[server_migrate] db.create_all() (non-destructive)..."
"$VENV_PY" - <<'PY'
from core.app import create_app
from core.extensions import db

app = create_app(register_blueprints=False)
with app.app_context():
    db.create_all()
print("OK: db.create_all")
PY

echo "[server_migrate] check alembic_version..."
HAS_ALEMBIC="$("$VENV_PY" - <<'PY'
from core.app import create_app
from sqlalchemy import create_engine, inspect

app = create_app(register_blueprints=False)
uri = app.config.get("SQLALCHEMY_DATABASE_URI")
engine = create_engine(uri)
inspector = inspect(engine)
print("1" if "alembic_version" in inspector.get_table_names() else "0")
PY
)"

if [ "$HAS_ALEMBIC" = "0" ]; then
  echo "[server_migrate] no alembic_version found; stamping to 0002_schema_fixes (adopt existing schema)"
  "$VENV_PY" -m alembic stamp 0002_schema_fixes
else
  echo "[server_migrate] alembic_version exists; skip stamp"
fi

echo "[server_migrate] alembic upgrade head..."
"$VENV_PY" -m alembic upgrade head

echo "[server_migrate] verify current revision equals the single head..."
"$VENV_PY" - <<'PY'
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from core.app import create_app
from sqlalchemy import create_engine

app = create_app(register_blueprints=False)
config = Config("alembic.ini")
script = ScriptDirectory.from_config(config)
expected_heads = tuple(script.get_heads())
if len(expected_heads) != 1:
    raise SystemExit(f"expected exactly one Alembic head, got: {expected_heads}")

engine = create_engine(app.config["SQLALCHEMY_DATABASE_URI"])
with engine.connect() as connection:
    current_heads = tuple(MigrationContext.configure(connection).get_current_heads())
if current_heads != expected_heads:
    raise SystemExit(
        f"database revision mismatch: current={current_heads}, expected={expected_heads}"
    )
print("OK: alembic current == single head", expected_heads[0])
PY

echo "[server_migrate] sanity check key tables..."
"$VENV_PY" - <<'PY'
from core.app import create_app
from sqlalchemy import create_engine, inspect

app = create_app(register_blueprints=False)
uri = app.config["SQLALCHEMY_DATABASE_URI"]
engine = create_engine(uri)
inspector = inspect(engine)
tables = set(inspector.get_table_names())

needed = {
    "pairs",
    "daily_status",
    "community_daily",
    "short_code_attempts",
    "debriefs",
    "api_tokens",
    "usage_events",
    "alert_deliveries",
    "location_cache",
    "miniprogram_snapshots",
    "miniprogram_identities",
    "miniprogram_sessions",
}
missing = sorted(needed - tables)
if missing:
    raise SystemExit("missing tables: " + ", ".join(missing))

required_columns = {
    "miniprogram_snapshots": {
        "snapshot_id",
        "fetched_at",
        "expires_at",
        "available",
        "source_status_json",
    },
    "miniprogram_identities": {
        "user_id",
        "openid_hash",
        "privacy_consent_version",
        "acquisition_source",
    },
    "miniprogram_sessions": {
        "identity_id",
        "user_id",
        "token_hash",
        "expires_at",
        "revoked_at",
    },
    "usage_events": {
        "event_type",
        "meta_json",
        "source",
        "created_at",
    },
    "alert_deliveries": {
        "alert_id",
        "user_id",
        "channel",
        "status",
        "attempt_count",
        "reviewed_at",
        "reviewed_by_user_id",
        "review_action",
    },
}
for table_name, expected in required_columns.items():
    present = {column["name"] for column in inspector.get_columns(table_name)}
    missing_columns = sorted(expected - present)
    if missing_columns:
        raise SystemExit(
            f"missing columns in {table_name}: " + ", ".join(missing_columns)
        )
print("OK: pilot tables present")
PY

echo "[server_migrate] done"
