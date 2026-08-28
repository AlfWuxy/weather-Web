# -*- coding: utf-8 -*-
"""WxPusher UID 所有权迁移回归。"""
import importlib
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import DBAPIError


ROOT_DIR = Path(__file__).resolve().parents[1]


def _create_app(monkeypatch, database_path):
    monkeypatch.setenv('DATABASE_URI', f'sqlite:///{database_path.as_posix()}')
    monkeypatch.setenv('SECRET_KEY', 'wx-ownership-migration-test-secret')
    monkeypatch.setenv('PAIR_TOKEN_PEPPER', 'wx-ownership-pair-pepper-value')
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

    return create_app()


def _alembic_config(app):
    config = Config(str(ROOT_DIR / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', app.config['SQLALCHEMY_DATABASE_URI'])
    config.set_main_option('script_location', str(ROOT_DIR / 'migrations'))
    return config


def test_wxpusher_ownership_boolean_updates_compile_for_supported_dialects():
    migration = importlib.import_module(
        'migrations.versions.0029_wxpusher_uid_ownership'
    )

    statements = (
        migration._duplicate_uid_revoke_statement(),
        migration._revoke_all_unproven_statement(),
        migration._revoke_all_for_downgrade_statement(),
    )
    for statement in statements:
        compiled_postgresql = str(statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={'literal_binds': True},
        )).lower()
        compiled_sqlite = str(statement.compile(
            dialect=sqlite.dialect(),
            compile_kwargs={'literal_binds': True},
        )).lower()
        assert 'push_enabled = false' in compiled_postgresql
        assert 'push_enabled = 0' in compiled_sqlite
    assert migration._normalized_server_default(
        {'default': "'0'::integer"}
    ) == '0'


@pytest.mark.parametrize(
    'index_sql',
    (
        'CREATE UNIQUE INDEX uq_users_wxpusher_uid ON users(username)',
        'CREATE INDEX uq_users_wxpusher_uid ON users(wxpusher_uid)',
    ),
)
def test_wxpusher_ownership_migration_rejects_wrong_named_index_before_write(
    monkeypatch,
    tmp_path,
    index_sql,
):
    database_path = tmp_path / 'wxpusher-wrong-index.db'
    app = _create_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    command.downgrade(config, '0028_authorized_community')
    with sqlite3.connect(database_path) as connection:
        connection.execute(index_sql)
        connection.execute(
            '''INSERT INTO users (
                   username, password_hash, role, auth_version,
                   account_origin, created_at, wxpusher_uid,
                   push_enabled, wxpusher_consent_version,
                   wxpusher_consented_at
               ) VALUES (
                   'wx_wrong_index', 'hash', 'user', 1,
                   'web', '2026-08-09 00:00:00', 'UID_WRONG_INDEX',
                   1, 'privacy-old', '2026-08-09 00:00:00'
               )'''
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='invalid_index=uq_users_wxpusher_uid'):
        command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0] == '0028_authorized_community'
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(users)')
        }
        assert 'wxpusher_uid_verified_at' not in columns
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert 'wxpusher_binding_challenges' not in tables
        assert connection.execute(
            '''SELECT wxpusher_uid, push_enabled,
                      wxpusher_consent_version, wxpusher_consented_at
               FROM users WHERE username = 'wx_wrong_index' '''
        ).fetchone() == (
            'UID_WRONG_INDEX',
            1,
            'privacy-old',
            '2026-08-09 00:00:00',
        )


def test_wxpusher_ownership_migration_rejects_global_index_name_collision(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / 'wxpusher-index-name-collision.db'
    app = _create_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    command.downgrade(config, '0028_authorized_community')
    with sqlite3.connect(database_path) as connection:
        connection.execute('CREATE TABLE wx_index_holder (id INTEGER PRIMARY KEY)')
        connection.execute(
            '''CREATE INDEX ix_wxpusher_binding_challenges_user_active
               ON wx_index_holder(id)'''
        )
        connection.execute(
            '''INSERT INTO users (
                   username, password_hash, role, auth_version,
                   account_origin, created_at, wxpusher_uid,
                   push_enabled, wxpusher_consent_version,
                   wxpusher_consented_at
               ) VALUES (
                   'wx_index_collision', 'hash', 'user', 1,
                   'web', '2026-08-09 00:00:00', 'UID_INDEX_COLLISION',
                   1, 'privacy-old', '2026-08-09 00:00:00'
               )'''
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='index_name_conflict'):
        command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0] == '0028_authorized_community'
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(users)')
        }
        assert 'wxpusher_uid_verified_at' not in columns
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert 'wxpusher_binding_challenges' not in tables
        assert connection.execute(
            '''SELECT wxpusher_uid, push_enabled, wxpusher_consent_version
               FROM users WHERE username = 'wx_index_collision' '''
        ).fetchone() == ('UID_INDEX_COLLISION', 1, 'privacy-old')


def test_wxpusher_ownership_migration_rejects_invalid_verified_column(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / 'wxpusher-invalid-verified-column.db'
    app = _create_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    command.downgrade(config, '0028_authorized_community')
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE users ADD COLUMN wxpusher_uid_verified_at TEXT NOT NULL DEFAULT ''"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='invalid_column=wxpusher_uid_verified_at'):
        command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0] == '0028_authorized_community'
        column = next(
            row
            for row in connection.execute('PRAGMA table_info(users)')
            if row[1] == 'wxpusher_uid_verified_at'
        )
        assert column[2].upper() == 'TEXT'
        assert column[3] == 1


def test_wxpusher_ownership_migration_rejects_invalid_challenge_table(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / 'wxpusher-invalid-challenge-table.db'
    app = _create_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    command.downgrade(config, '0028_authorized_community')
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''CREATE TABLE wxpusher_binding_challenges (
                   id INTEGER PRIMARY KEY
               )'''
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='invalid_challenge_table'):
        command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0] == '0028_authorized_community'
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(users)')
        }
        assert 'wxpusher_uid_verified_at' not in columns
        challenge_columns = list(connection.execute(
            'PRAGMA table_info(wxpusher_binding_challenges)'
        ))
        assert [row[1] for row in challenge_columns] == ['id']


def test_wxpusher_ownership_upgrade_rolls_back_first_ddl_on_late_failure(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / 'wxpusher-upgrade-atomic.db'
    app = _create_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    command.downgrade(config, '0028_authorized_community')
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''INSERT INTO users (
                   username, password_hash, role, auth_version,
                   account_origin, created_at, wxpusher_uid,
                   push_enabled, wxpusher_consent_version,
                   wxpusher_consented_at
               ) VALUES (
                   'wx_atomic_upgrade', 'hash', 'user', 1,
                   'web', '2026-08-09 00:00:00', 'UID_ATOMIC_UPGRADE',
                   1, 'privacy-old', '2026-08-09 00:00:00'
               )'''
        )
        connection.execute(
            '''CREATE TRIGGER reject_wxpusher_upgrade_update
               BEFORE UPDATE ON users
               BEGIN
                   SELECT RAISE(ABORT, 'forced wxpusher upgrade failure');
               END'''
        )
        connection.commit()

    with pytest.raises(DBAPIError, match='forced wxpusher upgrade failure'):
        command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0] == '0028_authorized_community'
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(users)')
        }
        assert 'wxpusher_uid_verified_at' not in columns
        assert connection.execute(
            '''SELECT wxpusher_uid, push_enabled, wxpusher_consent_version
               FROM users WHERE username = 'wx_atomic_upgrade' '''
        ).fetchone() == ('UID_ATOMIC_UPGRADE', 1, 'privacy-old')


def test_wxpusher_ownership_downgrade_rolls_back_before_dropping_schema(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / 'wxpusher-downgrade-atomic.db'
    app = _create_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''INSERT INTO users (
                   username, password_hash, role, auth_version,
                   account_origin, created_at, wxpusher_uid,
                   wxpusher_uid_verified_at, push_enabled,
                   wxpusher_consent_version, wxpusher_consented_at
               ) VALUES (
                   'wx_atomic_downgrade', 'hash', 'user', 1,
                   'web', '2026-08-09 00:00:00', 'UID_ATOMIC_DOWNGRADE',
                   '2026-08-09 00:00:00', 1,
                   'privacy-current', '2026-08-09 00:00:00'
               )'''
        )
        connection.execute(
            '''CREATE TRIGGER reject_wxpusher_downgrade_update
               BEFORE UPDATE ON users
               BEGIN
                   SELECT RAISE(ABORT, 'forced wxpusher downgrade failure');
               END'''
        )
        connection.commit()

    with pytest.raises(DBAPIError, match='forced wxpusher downgrade failure'):
        command.downgrade(config, '0028_authorized_community')

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0] == '0029_wxpusher_uid_ownership'
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(users)')
        }
        assert 'wxpusher_uid_verified_at' in columns
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert 'wxpusher_binding_challenges' in tables
        indexes = {
            row[1]
            for row in connection.execute('PRAGMA index_list(users)')
        }
        assert 'uq_users_wxpusher_uid' in indexes
        assert connection.execute(
            '''SELECT wxpusher_uid, wxpusher_uid_verified_at,
                      push_enabled, wxpusher_consent_version
               FROM users WHERE username = 'wx_atomic_downgrade' '''
        ).fetchone() == (
            'UID_ATOMIC_DOWNGRADE',
            '2026-08-09 00:00:00',
            1,
            'privacy-current',
        )


def test_wxpusher_ownership_migration_revokes_unproven_history(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / 'wxpusher-ownership.db'
    app = _create_app(monkeypatch, database_path)
    runner = app.test_cli_runner()
    initialized = runner.invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    command.downgrade(config, '0028_authorized_community')
    with sqlite3.connect(database_path) as connection:
        for username, uid in (
            ('wx_duplicate_one', 'UID_DUPLICATE'),
            ('wx_duplicate_two', 'UID_DUPLICATE'),
            ('wx_trim_duplicate_one', ' UID_TRIM_DUPLICATE '),
            ('wx_trim_duplicate_two', 'UID_TRIM_DUPLICATE'),
            ('wx_historical_unique', 'UID_HISTORICAL_UNIQUE'),
        ):
            connection.execute(
                '''INSERT INTO users (
                       username, password_hash, role, auth_version,
                       account_origin, created_at, wxpusher_uid,
                       push_enabled, wxpusher_consent_version,
                       wxpusher_consented_at
                   ) VALUES (?, 'hash', 'user', 1, 'web', ?, ?, 1, ?, ?)''',
                (
                    username,
                    '2026-08-09 00:00:00',
                    uid,
                    'privacy-old',
                    '2026-08-09 00:00:00',
                ),
            )
        connection.commit()

    command.upgrade(config, 'head')
    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]
        assert revision == '0032_weather_alert_provenance'
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(users)')
        }
        assert 'wxpusher_uid_verified_at' in columns
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert 'wxpusher_binding_challenges' in tables
        rows = connection.execute(
            '''SELECT username, wxpusher_uid, wxpusher_uid_verified_at,
                      push_enabled, wxpusher_consent_version,
                      wxpusher_consented_at
               FROM users ORDER BY username'''
        ).fetchall()
        assert rows == [
            ('wx_duplicate_one', None, None, 0, None, None),
            ('wx_duplicate_two', None, None, 0, None, None),
            (
                'wx_historical_unique',
                'UID_HISTORICAL_UNIQUE',
                None,
                0,
                None,
                None,
            ),
            ('wx_trim_duplicate_one', None, None, 0, None, None),
            ('wx_trim_duplicate_two', None, None, 0, None, None),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''INSERT INTO users (
                       username, password_hash, role, auth_version,
                       account_origin, created_at, wxpusher_uid, push_enabled
                   ) VALUES (
                       'wx_duplicate_after', 'hash', 'user', 1,
                       'web', '2026-08-09 00:00:00',
                       'UID_HISTORICAL_UNIQUE', 0
                   )'''
            )

    command.downgrade(config, '0028_authorized_community')
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(users)')
        }
        assert 'wxpusher_uid_verified_at' not in columns
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert 'wxpusher_binding_challenges' not in tables
        assert connection.execute(
            'SELECT COUNT(*) FROM users WHERE push_enabled != 0 OR wxpusher_uid IS NOT NULL'
        ).fetchone()[0] == 0
