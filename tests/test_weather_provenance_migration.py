# -*- coding: utf-8 -*-
"""0030-0032 天气可信度迁移的结构、原子性与降级保护。"""

import importlib
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy import inspect


ROOT_DIR = Path(__file__).resolve().parents[1]
HEAD_REVISION = '0032_weather_alert_provenance'
BASE_REVISION = '0029_wxpusher_uid_ownership'


def _create_app(monkeypatch, database_path):
    monkeypatch.setenv('DATABASE_URI', f'sqlite:///{database_path.as_posix()}')
    monkeypatch.setenv('SECRET_KEY', 'weather-provenance-migration-secret')
    monkeypatch.setenv('PAIR_TOKEN_PEPPER', 'weather-provenance-migration-pepper')
    monkeypatch.setenv('DEBUG', 'true')
    monkeypatch.setenv('DEMO_MODE', '1')
    monkeypatch.setenv('RATE_LIMIT_STORAGE_URI', 'memory://')
    monkeypatch.setenv('REDIS_URL', '')
    monkeypatch.setenv('QWEATHER_KEY', '')
    monkeypatch.setenv('QWEATHER_API_BASE', '')
    monkeypatch.setenv('AMAP_KEY', '')
    monkeypatch.setenv('SILICONFLOW_API_KEY', '')
    monkeypatch.setenv('SENTRY_DSN', '')
    monkeypatch.delenv('DEFAULT_ADMIN_USERNAME', raising=False)
    monkeypatch.delenv('DEFAULT_ADMIN_PASSWORD', raising=False)

    from core.app import create_app

    return create_app()


def _initialize(monkeypatch, database_path):
    app = _create_app(monkeypatch, database_path)
    result = app.test_cli_runner().invoke(args=['init-db'])
    assert result.exit_code == 0, result.output
    config = Config(str(ROOT_DIR / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', app.config['SQLALCHEMY_DATABASE_URI'])
    config.set_main_option('script_location', str(ROOT_DIR / 'migrations'))

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    return config


def _revision(connection):
    return connection.execute(
        'SELECT version_num FROM alembic_version'
    ).fetchone()[0]


def _column_names(connection, table_name):
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info({table_name})')
    }


def test_revision_chain_has_single_expected_head():
    migration_0030 = importlib.import_module(
        'migrations.versions.0030_weather_data_provenance'
    )
    migration_0031 = importlib.import_module(
        'migrations.versions.0031_air_quality_observed_at'
    )
    migration_0032 = importlib.import_module(
        'migrations.versions.0032_weather_alert_provenance'
    )

    assert migration_0030.down_revision == BASE_REVISION
    assert migration_0031.down_revision == migration_0030.revision
    assert migration_0032.down_revision == migration_0031.revision

    from alembic.script import ScriptDirectory

    config = Config(str(ROOT_DIR / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT_DIR / 'migrations'))
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD_REVISION]


def test_upgrade_from_0029_is_idempotent_and_keeps_old_rows_fail_closed(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / 'weather-provenance-upgrade.db'
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, BASE_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO weather_data (date, location) VALUES ('2026-08-27', '都昌县')"
        )
        connection.execute(
            """INSERT INTO weather_alerts (
                   alert_date, location, alert_type, alert_level, dedupe_key
               ) VALUES (?, ?, ?, ?, ?)""",
            ('2026-08-27 08:00:00', '都昌县', '高温', '黄色', 'a' * 64),
        )
        connection.commit()

    command.upgrade(config, 'head')
    command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == HEAD_REVISION
        weather_columns = _column_names(connection, 'weather_data')
        alert_columns = _column_names(connection, 'weather_alerts')
        weather_row = connection.execute(
            """SELECT data_source, observed_at, air_observed_at,
                      quality_version, air_quality_available
               FROM weather_data"""
        ).fetchone()
        alert_row = connection.execute(
            """SELECT source, is_official, starts_at, ends_at, dedupe_key
               FROM weather_alerts"""
        ).fetchone()
        alert_indexes = {
            row[1]: row
            for row in connection.execute('PRAGMA index_list(weather_alerts)')
        }

    assert {
        'data_source',
        'observed_at',
        'air_observed_at',
        'quality_version',
        'air_quality_available',
    } <= weather_columns
    assert {'source', 'is_official', 'starts_at', 'ends_at'} <= alert_columns
    assert weather_row == (None, None, None, 0, 0)
    assert alert_row == (None, 0, None, None, 'a' * 64)
    assert alert_indexes['uq_weather_alerts_dedupe_key'][2] == 1


def test_upgrade_rejects_missing_table_before_revision_change(monkeypatch, tmp_path):
    database_path = tmp_path / 'weather-provenance-missing-table.db'
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute('DROP TABLE weather_data')
        connection.commit()

    with pytest.raises(RuntimeError, match=r"missing_tables=\['weather_data'\]"):
        command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == BASE_REVISION


def test_upgrade_rejects_unexpected_nullable_default(monkeypatch, tmp_path):
    database_path = tmp_path / 'weather-provenance-wrong-default.db'
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, BASE_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE weather_data ADD COLUMN data_source VARCHAR(32) DEFAULT 'Legacy'"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='invalid_column=data_source'):
        command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == BASE_REVISION
        assert 'observed_at' not in _column_names(connection, 'weather_data')


def test_0030_requires_named_date_location_unique_constraint():
    migration = importlib.import_module(
        'migrations.versions.0030_weather_data_provenance'
    )
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    sa.Table(
        'weather_data',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True, nullable=False),
        sa.Column('date', sa.Date, nullable=False),
        sa.Column('location', sa.String(100), nullable=False),
    )
    metadata.create_all(engine)
    inspector = inspect(engine)
    columns = {
        column['name']: column
        for column in inspector.get_columns('weather_data')
    }
    with pytest.raises(RuntimeError, match='invalid_weather_data_baseline'):
        migration._validate_weather_data_baseline(inspector, columns)


def test_0030_accepts_postgresql_serial_and_constraint_backing_index():
    migration = importlib.import_module(
        'migrations.versions.0030_weather_data_provenance'
    )

    class PostgreSQLInspector:
        def get_table_names(self):
            return ['weather_data']

        def get_columns(self, _table_name):
            return [
                {
                    'name': 'id',
                    'type': sa.Integer(),
                    'nullable': False,
                    'primary_key': 1,
                    'default': "nextval('weather_data_id_seq'::regclass)",
                    'identity': None,
                },
                {
                    'name': 'date',
                    'type': sa.Date(),
                    'nullable': False,
                    'primary_key': 0,
                    'default': None,
                },
                {
                    'name': 'location',
                    'type': sa.String(100),
                    'nullable': False,
                    'primary_key': 0,
                    'default': None,
                },
            ]

        def get_unique_constraints(self, _table_name):
            return [{
                'name': migration.UNIQUE_NAME,
                'column_names': ['date', 'location'],
            }]

        def get_indexes(self, _table_name):
            return [{
                'name': migration.UNIQUE_NAME,
                'column_names': ['date', 'location'],
                'unique': True,
                'duplicates_constraint': migration.UNIQUE_NAME,
            }]

    inspector = PostgreSQLInspector()
    columns = {
        column['name']: column
        for column in inspector.get_columns('weather_data')
    }
    migration._validate_weather_data_baseline(inspector, columns)


def test_postgresql_datetime_columns_require_timezone_aware_types():
    migration_0030 = importlib.import_module(
        'migrations.versions.0030_weather_data_provenance'
    )
    migration_0032 = importlib.import_module(
        'migrations.versions.0032_weather_alert_provenance'
    )

    with pytest.raises(RuntimeError, match='invalid_column=observed_at'):
        migration_0030._validate_existing(
            {
                'observed_at': {
                    'type': sa.DateTime(timezone=False),
                    'nullable': True,
                    'default': None,
                }
            },
            'postgresql',
        )
    with pytest.raises(RuntimeError, match='invalid_column=starts_at'):
        migration_0032._validate_existing(
            {
                'starts_at': {
                    'type': sa.DateTime(timezone=False),
                    'nullable': True,
                    'default': None,
                }
            },
            'postgresql',
        )


def test_partial_schema_protected_counts_only_reference_existing_columns():
    migration_0030 = importlib.import_module(
        'migrations.versions.0030_weather_data_provenance'
    )
    migration_0032 = importlib.import_module(
        'migrations.versions.0032_weather_alert_provenance'
    )
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE TABLE weather_data (id INTEGER PRIMARY KEY, data_source VARCHAR(32))'
        )
        connection.exec_driver_sql(
            'CREATE TABLE weather_alerts (id INTEGER PRIMARY KEY, source VARCHAR(50))'
        )
        connection.exec_driver_sql(
            'INSERT INTO weather_data (data_source) VALUES (NULL)'
        )
        connection.exec_driver_sql(
            'INSERT INTO weather_alerts (source) VALUES (NULL)'
        )
        assert migration_0030._protected_row_count(
            connection,
            {'id': {}, 'data_source': {}},
        ) == 0
        assert migration_0032._protected_row_count(
            connection,
            {'id': {}, 'source': {}},
        ) == 0
        connection.exec_driver_sql(
            "UPDATE weather_data SET data_source='QWeather'"
        )
        connection.exec_driver_sql(
            "UPDATE weather_alerts SET source='AppThreshold'"
        )
        assert migration_0030._protected_row_count(
            connection,
            {'id': {}, 'data_source': {}},
        ) == 1
        assert migration_0032._protected_row_count(
            connection,
            {'id': {}, 'source': {}},
        ) == 1


def test_0032_rejects_wrong_dedupe_index_before_ddl(monkeypatch, tmp_path):
    database_path = tmp_path / 'weather-alert-wrong-index.db'
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, '0031_air_quality_observed_at')
    with sqlite3.connect(database_path) as connection:
        connection.execute('DROP INDEX uq_weather_alerts_dedupe_key')
        connection.execute(
            'CREATE INDEX uq_weather_alerts_dedupe_key ON weather_alerts(alert_type)'
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='invalid_dedupe_baseline'):
        command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == '0031_air_quality_observed_at'
        assert 'source' not in _column_names(connection, 'weather_alerts')


def test_0032_rejects_wrong_dedupe_length():
    migration = importlib.import_module(
        'migrations.versions.0032_weather_alert_provenance'
    )
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    table = sa.Table(
        'weather_alerts',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('dedupe_key', sa.String(32), nullable=True),
    )
    sa.Index(
        'uq_weather_alerts_dedupe_key',
        table.c.dedupe_key,
        unique=True,
    )
    metadata.create_all(engine)
    inspector = inspect(engine)
    columns = {
        column['name']: column
        for column in inspector.get_columns('weather_alerts')
    }
    with pytest.raises(RuntimeError, match='invalid_dedupe_baseline'):
        migration._validate_dedupe_baseline(inspector, columns)


def test_mid_upgrade_failure_rolls_back_schema_and_revision(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / 'weather-provenance-atomic.db'
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, BASE_REVISION)
    migration = importlib.import_module(
        'migrations.versions.0030_weather_data_provenance'
    )
    original_add_column = migration.op.add_column
    calls = {'count': 0}

    def fail_second_add_column(*args, **kwargs):
        calls['count'] += 1
        if calls['count'] == 2:
            raise RuntimeError('injected weather provenance DDL failure')
        return original_add_column(*args, **kwargs)

    monkeypatch.setattr(migration.op, 'add_column', fail_second_add_column)
    with pytest.raises(RuntimeError, match='injected weather provenance DDL failure'):
        command.upgrade(config, 'head')

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == BASE_REVISION
        columns = _column_names(connection, 'weather_data')
    assert not {
        'data_source',
        'observed_at',
        'quality_version',
        'air_quality_available',
    } & columns


def test_downgrade_refuses_to_drop_nondefault_provenance(monkeypatch, tmp_path):
    database_path = tmp_path / 'weather-provenance-downgrade-guard.db'
    config = _initialize(monkeypatch, database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO weather_data (
                   date, location, data_source, observed_at, quality_version
               ) VALUES (?, ?, ?, ?, ?)""",
            ('2026-08-28', '都昌县', 'QWeather', '2026-08-28 08:00:00', 1),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='provenance columns were preserved'):
        command.downgrade(config, BASE_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == HEAD_REVISION
        assert 'data_source' in _column_names(connection, 'weather_data')
        assert 'source' in _column_names(connection, 'weather_alerts')


def test_0031_direct_downgrade_preflights_0030_before_first_ddl(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / 'air-observation-chain-preflight.db'
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, '0031_air_quality_observed_at')
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO weather_data (
                   date, location, data_source, observed_at, quality_version
               ) VALUES (?, ?, ?, ?, ?)""",
            ('2026-08-28', '都昌县', 'QWeather', '2026-08-28 08:00:00', 1),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='provenance columns were preserved'):
        command.downgrade(config, BASE_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == '0031_air_quality_observed_at'
        assert 'air_observed_at' in _column_names(connection, 'weather_data')
