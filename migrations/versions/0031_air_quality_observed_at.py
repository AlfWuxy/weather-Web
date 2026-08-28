"""增加空气质量独立观测时间

Revision ID: 0031_air_quality_observed_at
Revises: 0030_weather_data_provenance
Create Date: 2026-08-28 00:10:00.000000
"""

import importlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0031_air_quality_observed_at'
down_revision = '0030_weather_data_provenance'
branch_labels = None
depends_on = None


COLUMN_NAME = 'air_observed_at'
PREVIOUS_COLUMN_SPECS = {
    'data_source': (sa.String, 32, True, ''),
    'observed_at': (sa.DateTime, None, True, ''),
    'quality_version': (sa.Integer, None, False, '0'),
    'air_quality_available': (sa.Boolean, None, False, '0'),
}


def _begin_sqlite_write_transaction(bind):
    """SQLite 首个 DDL 前开启真实写事务。"""
    if str(bind.dialect.name or '').lower() != 'sqlite':
        return
    driver_connection = getattr(
        getattr(bind, 'connection', None),
        'driver_connection',
        None,
    )
    if driver_connection is not None and getattr(
        driver_connection,
        'in_transaction',
        False,
    ):
        return
    bind.exec_driver_sql('BEGIN IMMEDIATE')


def _columns(inspector):
    if 'weather_data' not in set(inspector.get_table_names()):
        raise RuntimeError(
            "air observation migration aborted: missing_tables=['weather_data']"
        )
    return {
        column['name']: column
        for column in inspector.get_columns('weather_data')
    }


def _normalized_server_default(column):
    value = str(column.get('default') or '').strip()
    value = value.split('::', 1)[0]
    value = value.strip().strip("'\"() ").lower()
    if value in {'false', '0.0'}:
        return '0'
    return value


def _datetime_timezone_is_valid(column, dialect_name):
    if str(dialect_name or '').lower() != 'postgresql':
        return True
    return getattr(column.get('type'), 'timezone', False) is True


def _validate_previous_columns(columns, dialect_name=None):
    """0031 只能建立在完整的 0030 provenance 基线上。"""
    for name, (expected_type, expected_length, nullable, default) in (
        PREVIOUS_COLUMN_SPECS.items()
    ):
        column = columns.get(name)
        if column is None:
            raise RuntimeError(
                'air observation migration aborted: '
                f'missing_previous_column={name}'
            )
        column_type = column.get('type')
        invalid = (
            not isinstance(column_type, expected_type)
            or column.get('nullable') is not nullable
            or _normalized_server_default(column) != default
        )
        if expected_length is not None:
            invalid = invalid or getattr(column_type, 'length', None) != expected_length
        if expected_type is sa.DateTime:
            invalid = invalid or not _datetime_timezone_is_valid(
                column,
                dialect_name,
            )
        if invalid:
            raise RuntimeError(
                'air observation migration aborted: '
                f'invalid_previous_column={name}'
            )


def _validate_existing(columns, dialect_name=None):
    column = columns.get(COLUMN_NAME)
    if column is None:
        return
    if (
        not isinstance(column.get('type'), sa.DateTime)
        or column.get('nullable') is not True
        or _normalized_server_default(column)
        or not _datetime_timezone_is_valid(column, dialect_name)
    ):
        raise RuntimeError(
            f'air observation migration aborted: invalid_column={COLUMN_NAME}'
        )


def _preflight_lower_downgrade(bind):
    """0031 直降 0029 时先检查所有后续降级保护，避免半回滚。"""
    context = op.get_context()
    environment_context = getattr(context, 'environment_context', None)
    if environment_context is None:
        return
    destination = environment_context.get_revision_argument()
    try:
        planned = tuple(environment_context.script.iterate_revisions(
            context.get_current_heads(),
            destination,
            select_for_downgrade=True,
        ))
    except Exception as exc:
        raise RuntimeError(
            'air observation migration aborted: '
            'unable_to_resolve_downgrade_plan'
        ) from exc
    planned_ids = {item.revision for item in planned}
    inspector = inspect(bind)

    if '0030_weather_data_provenance' in planned_ids:
        migration_0030 = importlib.import_module(
            'migrations.versions.0030_weather_data_provenance'
        )
        columns = migration_0030._columns(inspector)
        migration_0030._validate_weather_data_baseline(inspector, columns)
        migration_0030._validate_existing(columns, bind.dialect.name)
        protected_count = migration_0030._protected_row_count(bind, columns)
        if protected_count:
            raise RuntimeError(
                'weather provenance downgrade aborted: '
                f'protected_count={protected_count}; provenance columns were preserved'
            )

    if '0029_wxpusher_uid_ownership' in planned_ids:
        migration_0029 = importlib.import_module(
            'migrations.versions.0029_wxpusher_uid_ownership'
        )
        migration_0029._preflight_lower_downgrade(bind, inspector)


def upgrade():
    bind = op.get_bind()
    columns = _columns(inspect(bind))
    _validate_previous_columns(columns, bind.dialect.name)
    _validate_existing(columns, bind.dialect.name)
    if COLUMN_NAME in columns:
        return

    _begin_sqlite_write_transaction(bind)
    # 历史行没有独立空气观测来源，保持 NULL，禁止借用天气时间回填。
    op.add_column(
        'weather_data',
        sa.Column(COLUMN_NAME, sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    bind = op.get_bind()
    columns = _columns(inspect(bind))
    _validate_previous_columns(columns, bind.dialect.name)
    _validate_existing(columns, bind.dialect.name)
    if COLUMN_NAME not in columns:
        return
    table = sa.table(
        'weather_data',
        sa.column(COLUMN_NAME, sa.DateTime),
    )
    protected_count = int(bind.execute(
        sa.select(sa.func.count()).select_from(table).where(
            table.c[COLUMN_NAME].is_not(None)
        )
    ).scalar_one())
    if protected_count:
        raise RuntimeError(
            'air observation downgrade aborted: '
            f'protected_count={protected_count}; observation column was preserved'
        )
    _preflight_lower_downgrade(bind)

    _begin_sqlite_write_transaction(bind)
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table('weather_data') as batch_op:
            batch_op.drop_column(COLUMN_NAME)
        return
    op.drop_column('weather_data', COLUMN_NAME)
