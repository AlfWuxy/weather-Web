# -*- coding: utf-8 -*-
"""0028 独立运营社区授权迁移的降级保护。"""

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
REVISION = '0028_authorized_community'
PREVIOUS_REVISION = '0027_cross_platform_identity'


def _initialize(monkeypatch, database_path):
    monkeypatch.setenv('DATABASE_URI', f'sqlite:///{database_path.as_posix()}')
    monkeypatch.setenv('SECRET_KEY', 'authorized-community-migration-secret')
    monkeypatch.setenv('PAIR_TOKEN_PEPPER', 'authorized-community-pair-pepper')
    monkeypatch.setenv('DEBUG', 'true')
    monkeypatch.setenv('DEMO_MODE', '1')
    monkeypatch.setenv('FEATURE_WXPUSHER', '0')
    monkeypatch.setenv('WXPUSHER_APP_TOKEN', '')
    monkeypatch.setenv('RATE_LIMIT_STORAGE_URI', 'memory://')
    monkeypatch.setenv('REDIS_URL', '')
    monkeypatch.setenv('QWEATHER_KEY', '')
    monkeypatch.setenv('QWEATHER_API_BASE', '')
    monkeypatch.setenv('AMAP_KEY', '')
    monkeypatch.setenv('SILICONFLOW_API_KEY', '')
    monkeypatch.setenv('SENTRY_DSN', '')

    from core.app import create_app
    from core.extensions import db

    app = create_app()
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    config = Config(str(ROOT_DIR / 'alembic.ini'))
    config.set_main_option(
        'sqlalchemy.url',
        app.config['SQLALCHEMY_DATABASE_URI'],
    )
    config.set_main_option('script_location', str(ROOT_DIR / 'migrations'))
    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    # 先回到 0027，再单独升级 0028，避免其他迁移掩盖本测试语义。
    command.downgrade(config, PREVIOUS_REVISION)
    command.upgrade(config, REVISION)
    return config


def _revision(connection):
    return connection.execute(
        'SELECT version_num FROM alembic_version'
    ).fetchone()[0]


@pytest.mark.parametrize(
    ('role', 'authorized_community', 'expected_error'),
    (
        ('community', None, 'operating_role_count=1'),
        ('user', '合法社区', 'authorized_community_count=1'),
    ),
)
def test_downgrade_rejects_unrepresentable_authorization_before_any_write(
    monkeypatch,
    tmp_path,
    role,
    authorized_community,
    expected_error,
):
    database_path = tmp_path / f'authorized-community-{role}.db'
    config = _initialize(monkeypatch, database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''INSERT INTO users (
                   username, password_hash, role, auth_version,
                   account_origin, created_at, authorized_community
               ) VALUES (?, 'hash', ?, 1, 'web', ?, ?)''',
            (
                f'operator-{role}',
                role,
                '2026-08-09 00:00:00',
                authorized_community,
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match=expected_error):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == REVISION
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(users)')
        }
        assert 'authorized_community' in columns
        assert connection.execute(
            '''SELECT role, authorized_community FROM users
               WHERE username = ?''',
            (f'operator-{role}',),
        ).fetchone() == (role, authorized_community)


def test_downgrade_drops_empty_authorization_column(monkeypatch, tmp_path):
    database_path = tmp_path / 'authorized-community-empty.db'
    config = _initialize(monkeypatch, database_path)

    command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == PREVIOUS_REVISION
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(users)')
        }
        assert 'authorized_community' not in columns
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE role IN ('community', 'caregiver')"
        ).fetchone()[0] == 0
