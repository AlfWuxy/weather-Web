# -*- coding: utf-8 -*-
"""天气与空气质量 provenance 迁移回归测试。"""
import importlib
from datetime import date, datetime

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


def test_weather_provenance_migration_is_current_head():
    script = ScriptDirectory.from_config(Config('alembic.ini'))
    assert script.get_heads() == ['0018_health_consent_care']


def test_weather_provenance_migration_keeps_legacy_rows_untrusted(monkeypatch):
    migration = importlib.import_module(
        'migrations.versions.0012_weather_data_provenance'
    )
    engine = sa.create_engine('sqlite:///:memory:')
    metadata = sa.MetaData()
    legacy_weather = sa.Table(
        'weather_data',
        metadata,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('location', sa.String(length=100), nullable=False),
        sa.Column('temperature', sa.Float()),
        sa.Column('aqi', sa.Integer()),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(legacy_weather.insert().values(
            id=1,
            date=date(2026, 8, 26),
            location='都昌县',
            temperature=32.0,
            aqi=0,
        ))
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, 'op', operations)

        migration.upgrade()
        # 重复执行不应再次添加列。
        migration.upgrade()

        columns = {
            column['name']: column
            for column in sa.inspect(connection).get_columns('weather_data')
        }
        row = connection.execute(sa.text(
            'SELECT data_source, observed_at, quality_version, '
            'air_quality_available FROM weather_data WHERE id = 1'
        )).mappings().one()

    assert {'data_source', 'observed_at', 'quality_version', 'air_quality_available'} <= set(columns)
    assert row['data_source'] is None
    assert row['observed_at'] is None
    assert row['quality_version'] == 0
    assert row['air_quality_available'] in (0, False)


def test_air_observation_migration_keeps_legacy_rows_null(monkeypatch):
    migration = importlib.import_module(
        'migrations.versions.0013_air_quality_observed_at'
    )
    engine = sa.create_engine('sqlite:///:memory:')
    metadata = sa.MetaData()
    legacy_weather = sa.Table(
        'weather_data',
        metadata,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('location', sa.String(length=100), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('air_quality_available', sa.Boolean(), nullable=False),
    )
    metadata.create_all(engine)
    weather_observed_at = datetime(2026, 8, 28, 6, 0)

    with engine.begin() as connection:
        connection.execute(legacy_weather.insert().values(
            id=1,
            date=date(2026, 8, 28),
            location='都昌县',
            observed_at=weather_observed_at,
            air_quality_available=True,
        ))
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, 'op', operations)

        migration.upgrade()
        # 重复执行不应再次添加列，也不能用天气时间回填空气时间。
        migration.upgrade()

        columns = {
            column['name']: column
            for column in sa.inspect(connection).get_columns('weather_data')
        }
        row = connection.execute(sa.text(
            'SELECT observed_at, air_observed_at '
            'FROM weather_data WHERE id = 1'
        )).mappings().one()

    assert 'air_observed_at' in columns
    assert row['observed_at'] is not None
    assert row['air_observed_at'] is None


def test_alert_provenance_migration_keeps_legacy_rows_non_official(monkeypatch):
    migration = importlib.import_module(
        'migrations.versions.0014_weather_alert_provenance'
    )
    engine = sa.create_engine('sqlite:///:memory:')
    metadata = sa.MetaData()
    legacy_alerts = sa.Table(
        'weather_alerts',
        metadata,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('alert_date', sa.DateTime(), nullable=True),
        sa.Column('location', sa.String(length=100), nullable=True),
        sa.Column('alert_type', sa.String(length=50), nullable=True),
        sa.Column('alert_level', sa.String(length=20), nullable=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(legacy_alerts.insert().values(
            id=1,
            alert_date=datetime(2026, 8, 27, 6, 0),
            location='都昌县',
            alert_type='轻度空气污染',
            alert_level='蓝色预警',
        ))
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, 'op', operations)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column['name']: column
            for column in sa.inspect(connection).get_columns('weather_alerts')
        }
        row = connection.execute(sa.text(
            'SELECT source, is_official, starts_at, ends_at '
            'FROM weather_alerts WHERE id = 1'
        )).mappings().one()

    assert {'source', 'is_official', 'starts_at', 'ends_at'} <= set(columns)
    assert row['source'] is None
    assert row['is_official'] in (0, False)
    assert row['starts_at'] is None
    assert row['ends_at'] is None
