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

echo "[server_migrate] inspect existing database before schema writes..."
DATABASE_STATE="$("$VENV_PY" - <<'PY'
from core.app import create_app
from sqlalchemy import create_engine, inspect

app = create_app(register_blueprints=False)
uri = app.config.get("SQLALCHEMY_DATABASE_URI")
engine = create_engine(uri)
inspector = inspect(engine)
tables = set(inspector.get_table_names())
business_tables = tables - {"alembic_version"}
has_alembic = "alembic_version" in tables

# 没有版本表的旧库只允许从完整核心基线接管，避免 create_all 掩盖数据表损坏。
if business_tables and not has_alembic:
    required_legacy_tables = {
        "users",
        "family_members",
        "health_diary",
        "medication_reminders",
        "health_risk_assessments",
    }
    missing = sorted(required_legacy_tables - business_tables)
    if missing:
        raise SystemExit(
            "existing database baseline missing tables before create_all: "
            + ", ".join(missing)
        )

print(f"{int(has_alembic)}:{int(bool(business_tables))}")
PY
)"
HAS_ALEMBIC="${DATABASE_STATE%%:*}"
HAS_BUSINESS_TABLES="${DATABASE_STATE##*:}"

# 全新数据库或待接管的无版本旧库需要模型基线；已纳入 Alembic 的数据库只走迁移。
if [ "$HAS_ALEMBIC" = "0" ]; then
  echo "[server_migrate] db.create_all() for fresh or legacy baseline..."
  "$VENV_PY" - <<'PY'
from core.app import create_app
from core.extensions import db

app = create_app(register_blueprints=False)
with app.app_context():
    db.create_all()
print("OK: db.create_all")
PY
else
  echo "[server_migrate] versioned database detected; skip db.create_all"
fi

if [ "$HAS_ALEMBIC" = "0" ]; then
  if [ "$HAS_BUSINESS_TABLES" = "1" ]; then
    echo "[server_migrate] adopting validated legacy schema at 0002_schema_fixes"
  else
    echo "[server_migrate] fresh schema created; stamping to 0002_schema_fixes"
  fi
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
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect

app = create_app(register_blueprints=False)
uri = app.config["SQLALCHEMY_DATABASE_URI"]
engine = create_engine(uri)
inspector = inspect(engine)
tables = set(inspector.get_table_names())

needed = {
    "users",
    "pairs",
    "daily_status",
    "community_daily",
    "health_diary",
    "medication_reminders",
    "health_risk_assessments",
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
    "users": {
        "auth_version",
    },
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
    "weather_alerts": {
        "dedupe_key",
    },
    "debriefs": {
        "owner_user_id",
        "origin_pair_id",
    },
}
for table_name, expected in required_columns.items():
    present = {column["name"] for column in inspector.get_columns(table_name)}
    missing_columns = sorted(expected - present)
    if missing_columns:
        raise SystemExit(
            f"missing columns in {table_name}: " + ", ".join(missing_columns)
        )

user_columns = {
    column["name"]: column
    for column in inspector.get_columns("users")
}
auth_version_column = user_columns["auth_version"]
if auth_version_column.get("nullable") is not False:
    raise SystemExit("users.auth_version must be NOT NULL")
if not isinstance(auth_version_column.get("type"), sa.Integer):
    raise SystemExit("users.auth_version must be INTEGER")
with engine.connect() as connection:
    invalid_auth_version_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM users "
                "WHERE auth_version IS NULL OR auth_version < 1"
            )
        ).scalar_one()
    )
if invalid_auth_version_count:
    raise SystemExit(
        "users.auth_version contains invalid rows: "
        f"{invalid_auth_version_count}"
    )

debrief_columns = {
    column["name"]: column
    for column in inspector.get_columns("debriefs")
}
delivery_columns = {
    column["name"]: column
    for column in inspector.get_columns("alert_deliveries")
}
if delivery_columns["attempt_count"].get("nullable"):
    raise SystemExit("alert_deliveries.attempt_count must be NOT NULL")

weather_alert_indexes = {
    item.get("name"): item
    for item in inspector.get_indexes("weather_alerts")
}
dedupe_index = weather_alert_indexes.get("uq_weather_alerts_dedupe_key")
if (
    dedupe_index is None
    or not dedupe_index.get("unique")
    or dedupe_index.get("column_names") != ["dedupe_key"]
):
    raise SystemExit("missing weather_alerts dedupe unique index")

if debrief_columns["owner_user_id"].get("nullable"):
    raise SystemExit("debriefs.owner_user_id must be NOT NULL")

debrief_foreign_keys = inspector.get_foreign_keys("debriefs")
debrief_owner_fk = next(
    (
        foreign_key
        for foreign_key in debrief_foreign_keys
        if foreign_key.get("constrained_columns") == ["owner_user_id"]
        and foreign_key.get("referred_table") == "users"
        and foreign_key.get("referred_columns") == ["id"]
    ),
    None,
)
if debrief_owner_fk is None:
    raise SystemExit("missing debriefs.owner_user_id foreign key")
debrief_origin_fk = next(
    (
        foreign_key
        for foreign_key in debrief_foreign_keys
        if foreign_key.get("constrained_columns") == ["origin_pair_id"]
        and foreign_key.get("referred_table") == "pairs"
        and foreign_key.get("referred_columns") == ["id"]
    ),
    None,
)
if debrief_origin_fk is None:
    raise SystemExit("missing debriefs.origin_pair_id foreign key")
debrief_display_fk = next(
    (
        foreign_key
        for foreign_key in debrief_foreign_keys
        if foreign_key.get("constrained_columns") == ["pair_id"]
        and foreign_key.get("referred_table") == "pairs"
        and foreign_key.get("referred_columns") == ["id"]
    ),
    None,
)
if debrief_display_fk is None:
    raise SystemExit("missing debriefs.pair_id foreign key")

for column_name, foreign_key in (
    ("origin_pair_id", debrief_origin_fk),
    ("pair_id", debrief_display_fk),
):
    ondelete = (foreign_key.get("options") or {}).get("ondelete") or ""
    if str(ondelete).upper().replace(" ", "") != "SETNULL":
        raise SystemExit(f"debriefs.{column_name} must use ON DELETE SET NULL")

debrief_indexes = {index.get("name") for index in inspector.get_indexes("debriefs")}
if "ix_debriefs_owner_user_id" not in debrief_indexes:
    raise SystemExit("missing ix_debriefs_owner_user_id")
if "ix_debriefs_origin_pair_id" not in debrief_indexes:
    raise SystemExit("missing ix_debriefs_origin_pair_id")

private_health_indexes = {
    "health_diary": (
        "ix_health_diary_owner_member_date_id",
        ["user_id", "member_id", "entry_date", "id"],
    ),
    "medication_reminders": (
        "ix_medication_owner_member_id",
        ["user_id", "member_id", "id"],
    ),
    "health_risk_assessments": (
        "ix_assessment_owner_member_date_id",
        ["user_id", "member_id", "assessment_date", "id"],
    ),
}
for table_name, (index_name, expected_columns) in private_health_indexes.items():
    indexes = {
        item.get("name"): item
        for item in inspector.get_indexes(table_name)
    }
    actual = indexes.get(index_name)
    actual_columns = (actual or {}).get("column_names") or []
    if actual_columns != expected_columns or bool((actual or {}).get("unique")):
        raise SystemExit(
            f"invalid private health index {index_name}: "
            f"actual_columns={actual_columns}, expected={expected_columns}, "
            f"unique={bool((actual or {}).get('unique'))}"
        )
print("OK: pilot tables present")
PY

echo "[server_migrate] done"
