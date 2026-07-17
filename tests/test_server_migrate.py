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


def test_server_migrate_reaches_single_head_and_required_schema(tmp_path):
    database = tmp_path / "health_weather.db"
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
            row[1]
            for row in connection.execute("PRAGMA table_info(alert_deliveries)")
        }

    assert current == expected_head
    assert foreign_key_errors == []
    assert "acquisition_source" in identity_columns
    assert {
        "attempt_count",
        "reviewed_at",
        "reviewed_by_user_id",
        "review_action",
    } <= delivery_columns
