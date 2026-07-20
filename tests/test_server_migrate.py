# -*- coding: utf-8 -*-
"""生产迁移脚本的端到端行为测试。"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server_migrate.sh"
PRIVATE_HEALTH_INDEXES = {
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


def _migration_environment(database):
    environment = os.environ.copy()
    environment.update(
        {
            "VENV_PY": sys.executable,
            "DATABASE_URI": f"sqlite:///{database}",
            "DEBUG": "true",
            "SECRET_KEY": "migration-test-secret-key-123456789",
            "PAIR_TOKEN_PEPPER": "migration-test-pair-pepper-123456789",
            "RATE_LIMIT_STORAGE_URI": "memory://",
            "REDIS_URL": "",
            "QWEATHER_AUTH_MODE": "disabled",
            "QWEATHER_KEY": "",
            "QWEATHER_API_BASE": "",
            "WXPUSHER_APP_TOKEN": "",
        }
    )
    return environment


def test_server_migrate_reaches_single_head_and_required_schema(tmp_path):
    database = tmp_path / "health_weather.db"
    environment = _migration_environment(database)

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected_head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    with sqlite3.connect(database) as connection:
        current = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        identity_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(miniprogram_identities)")
        }
        delivery_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(alert_deliveries)")
        }
        weather_alert_indexes = {
            row[1]: row
            for row in connection.execute("PRAGMA index_list(weather_alerts)")
        }
        debrief_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(debriefs)")
        }
        user_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(users)")
        }
        debrief_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(debriefs)")
        }
        debrief_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(debriefs)"
        ).fetchall()
        private_health_index_columns = {}
        for table_name, (index_name, _expected_columns) in PRIVATE_HEALTH_INDEXES.items():
            indexes = {
                row[1]
                for row in connection.execute(f"PRAGMA index_list({table_name})")
            }
            assert index_name in indexes
            private_health_index_columns[index_name] = [
                row[2]
                for row in connection.execute(f"PRAGMA index_info({index_name})")
            ]
            index_row = next(
                row
                for row in connection.execute(f"PRAGMA index_list({table_name})")
                if row[1] == index_name
            )
            assert index_row[2] == 0

    assert current == expected_head
    assert foreign_key_errors == []
    assert "acquisition_source" in identity_columns
    assert {
        "attempt_count",
        "reviewed_at",
        "reviewed_by_user_id",
        "review_action",
    } <= set(delivery_columns)
    assert delivery_columns["attempt_count"][3] == 1
    assert "INT" in user_columns["auth_version"][2].upper()
    assert user_columns["auth_version"][3] == 1
    assert weather_alert_indexes["uq_weather_alerts_dedupe_key"][2] == 1
    assert debrief_columns["owner_user_id"][3] == 1
    assert debrief_columns["origin_pair_id"][3] == 0
    assert "ix_debriefs_owner_user_id" in debrief_indexes
    assert "ix_debriefs_origin_pair_id" in debrief_indexes
    assert any(
        row[2] == "users" and row[3] == "owner_user_id" and row[4] == "id"
        for row in debrief_foreign_keys
    )
    origin_fk = next(
        row
        for row in debrief_foreign_keys
        if row[2] == "pairs" and row[3] == "origin_pair_id" and row[4] == "id"
    )
    display_fk = next(
        row
        for row in debrief_foreign_keys
        if row[2] == "pairs" and row[3] == "pair_id" and row[4] == "id"
    )
    assert origin_fk[6] == "SET NULL"
    assert display_fk[6] == "SET NULL"
    for _table_name, (index_name, expected_columns) in PRIVATE_HEALTH_INDEXES.items():
        assert private_health_index_columns[index_name] == expected_columns


def test_server_migrate_rejects_nullable_attempt_count_schema_drift(tmp_path):
    database = tmp_path / "nullable-attempt-count.db"
    environment = _migration_environment(database)
    initialized = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr

    with sqlite3.connect(database) as connection:
        create_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_deliveries'"
        ).fetchone()[0]
        nullable_sql = create_sql.replace(
            "attempt_count INTEGER DEFAULT '1' NOT NULL",
            "attempt_count INTEGER DEFAULT '1'",
        )
        if nullable_sql == create_sql:
            nullable_sql = create_sql.replace(
                "attempt_count INTEGER DEFAULT 1 NOT NULL",
                "attempt_count INTEGER DEFAULT 1",
            )
        assert nullable_sql != create_sql
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(alert_deliveries)")
        ]
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "ALTER TABLE alert_deliveries RENAME TO alert_deliveries_strict_backup"
        )
        connection.execute(nullable_sql)
        connection.execute(
            f"INSERT INTO alert_deliveries ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM alert_deliveries_strict_backup"
        )
        connection.execute("DROP TABLE alert_deliveries_strict_backup")
        connection.commit()

    rejected = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "alert_deliveries.attempt_count must be NOT NULL" in (
        rejected.stdout + rejected.stderr
    )


def test_server_migrate_does_not_recreate_missing_table_in_versioned_database(tmp_path):
    """已纳入版本管理的数据库缺表时必须失败，禁止 create_all 静默修成空表。"""
    database = tmp_path / "missing-health-table.db"
    environment = _migration_environment(database)
    initialized = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE medication_reminders")
        connection.commit()

    rejected = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert rejected.returncode != 0
    assert "missing tables: medication_reminders" in (
        rejected.stdout + rejected.stderr
    )
    with sqlite3.connect(database) as connection:
        remaining = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "medication_reminders" not in remaining


def test_server_migrate_rejects_invalid_auth_version_rows(tmp_path):
    """会话撤销版本异常时必须阻断发布，避免旧 Cookie 继续有效。"""
    database = tmp_path / "invalid-auth-version.db"
    environment = _migration_environment(database)
    initialized = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (username, password_hash, auth_version) "
            "VALUES (?, ?, ?)",
            ("invalid-auth-version", "unused-test-hash", 0),
        )
        connection.commit()

    rejected = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert rejected.returncode != 0
    assert "users.auth_version contains invalid rows: 1" in (
        rejected.stdout + rejected.stderr
    )
