"""增加天气提醒来源、官方性与有效期

Revision ID: 0032_weather_alert_provenance
Revises: 0031_air_quality_observed_at
Create Date: 2026-08-28 00:20:00.000000
"""

import importlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0032_weather_alert_provenance'
down_revision = '0031_air_quality_observed_at'
branch_labels = None
depends_on = None


COLUMN_SPECS = {
    'source': (sa.String, 50, True, None),
    'is_official': (sa.Boolean, None, False, '0'),
    'starts_at': (sa.DateTime, None, True, None),
    'ends_at': (sa.DateTime, None, True, None),
}
DEDUPE_COLUMN = 'dedupe_key'
DEDUPE_INDEX = 'uq_weather_alerts_dedupe_key'


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


def _normalized_server_default(column):
    value = str(column.get('default') or '').strip()
    value = value.split('::', 1)[0]
    value = value.strip().strip("'\"() ").lower()
    if value in {'false', '0.0'}:
        return '0'
    return value


def _columns(inspector):
    if 'weather_alerts' not in set(inspector.get_table_names()):
        raise RuntimeError(
            "alert provenance migration aborted: missing_tables=['weather_alerts']"
        )
    return {
        column['name']: column
        for column in inspector.get_columns('weather_alerts')
    }


def _index_has_predicate(index):
    for key, value in (index.get('dialect_options') or {}).items():
        if key.endswith('_where') and value is not None and str(value).strip():
            return True
    return False


def _datetime_timezone_is_valid(column, dialect_name):
    if str(dialect_name or '').lower() != 'postgresql':
        return True
    return getattr(column.get('type'), 'timezone', False) is True


def _validate_dedupe_baseline(inspector, columns):
    """保护 0020 引入的并发幂等键与唯一索引。"""
    column = columns.get(DEDUPE_COLUMN)
    column_type = column.get('type') if column is not None else None
    invalid = (
        column is None
        or not isinstance(column_type, sa.String)
        or getattr(column_type, 'length', None) != 64
        or column.get('nullable') is not True
        or _normalized_server_default(column)
    )

    named_constraints = [
        item
        for item in inspector.get_unique_constraints('weather_alerts')
        if item.get('name') == DEDUPE_INDEX
    ]
    named_indexes = [
        item
        for item in inspector.get_indexes('weather_alerts')
        if item.get('name') == DEDUPE_INDEX
    ]
    if named_constraints or len(named_indexes) != 1:
        invalid = True
    elif (
        not bool(named_indexes[0].get('unique'))
        or list(named_indexes[0].get('column_names') or []) != [DEDUPE_COLUMN]
        or _index_has_predicate(named_indexes[0])
    ):
        invalid = True

    if invalid:
        raise RuntimeError(
            'alert provenance migration aborted: invalid_dedupe_baseline'
        )


def _validate_existing(columns, dialect_name=None):
    for name, (expected_type, expected_length, nullable, default) in COLUMN_SPECS.items():
        column = columns.get(name)
        if column is None:
            continue
        column_type = column.get('type')
        invalid = (
            not isinstance(column_type, expected_type)
            or column.get('nullable') is not nullable
        )
        if expected_length is not None:
            invalid = invalid or getattr(column_type, 'length', None) != expected_length
        if expected_type is sa.DateTime:
            invalid = invalid or not _datetime_timezone_is_valid(
                column,
                dialect_name,
            )
        invalid = invalid or _normalized_server_default(column) != (default or '')
        if invalid:
            raise RuntimeError(
                'alert provenance migration aborted: '
                f'invalid_column={name}'
            )


def _protected_row_count(bind, columns=None):
    """只引用实际存在的 provenance 列，兼容受控 partial schema 回滚。"""
    existing = set(columns or {
        column['name']
        for column in inspect(bind).get_columns('weather_alerts')
    })
    definitions = {
        'source': sa.String,
        'is_official': sa.Boolean,
        'starts_at': sa.DateTime,
        'ends_at': sa.DateTime,
    }
    present = [name for name in definitions if name in existing]
    if not present:
        return 0
    table = sa.table(
        'weather_alerts',
        *(sa.column(name, definitions[name]) for name in present),
    )
    conditions = []
    if 'source' in existing:
        conditions.append(table.c.source.is_not(None))
    if 'is_official' in existing:
        conditions.append(table.c.is_official.is_(True))
    if 'starts_at' in existing:
        conditions.append(table.c.starts_at.is_not(None))
    if 'ends_at' in existing:
        conditions.append(table.c.ends_at.is_not(None))
    return int(bind.execute(
        sa.select(sa.func.count()).select_from(table).where(sa.or_(*conditions))
    ).scalar_one())


def _preflight_lower_downgrade(bind):
    """在首个 DDL 前预检本次跨版本降级，避免 SQLite 留下半回滚。"""
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
            'alert provenance migration aborted: '
            'unable_to_resolve_downgrade_plan'
        ) from exc
    planned_ids = {item.revision for item in planned}

    if '0031_air_quality_observed_at' in planned_ids:
        migration_0031 = importlib.import_module(
            'migrations.versions.0031_air_quality_observed_at'
        )
        columns = migration_0031._columns(inspect(bind))
        migration_0031._validate_previous_columns(columns, bind.dialect.name)
        migration_0031._validate_existing(columns, bind.dialect.name)
        table = sa.table(
            'weather_data',
            sa.column(migration_0031.COLUMN_NAME, sa.DateTime),
        )
        protected_count = int(bind.execute(
            sa.select(sa.func.count()).select_from(table).where(
                table.c[migration_0031.COLUMN_NAME].is_not(None)
            )
        ).scalar_one())
        if protected_count:
            raise RuntimeError(
                'air observation downgrade aborted: '
                f'protected_count={protected_count}; observation column was preserved'
            )

    if '0030_weather_data_provenance' in planned_ids:
        migration_0030 = importlib.import_module(
            'migrations.versions.0030_weather_data_provenance'
        )
        columns = migration_0030._columns(inspect(bind))
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
        migration_0029._preflight_lower_downgrade(bind, inspect(bind))


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = _columns(inspector)
    _validate_dedupe_baseline(inspector, columns)
    _validate_existing(columns, bind.dialect.name)
    missing = [name for name in COLUMN_SPECS if name not in columns]
    if not missing:
        return

    _begin_sqlite_write_transaction(bind)
    # 历史提醒默认非官方，不根据文案推断来源或有效期。
    if 'source' in missing:
        op.add_column(
            'weather_alerts',
            sa.Column('source', sa.String(length=50), nullable=True),
        )
    if 'is_official' in missing:
        op.add_column(
            'weather_alerts',
            sa.Column(
                'is_official',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if 'starts_at' in missing:
        op.add_column(
            'weather_alerts',
            sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
        )
    if 'ends_at' in missing:
        op.add_column(
            'weather_alerts',
            sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = _columns(inspector)
    _validate_dedupe_baseline(inspector, columns)
    _validate_existing(columns, bind.dialect.name)
    _preflight_lower_downgrade(bind)
    removable = [name for name in reversed(COLUMN_SPECS) if name in columns]
    if not removable:
        return
    protected_count = _protected_row_count(bind, columns)
    if protected_count:
        raise RuntimeError(
            'alert provenance downgrade aborted: '
            f'protected_count={protected_count}; provenance columns were preserved'
        )

    _begin_sqlite_write_transaction(bind)
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table('weather_alerts') as batch_op:
            for name in removable:
                batch_op.drop_column(name)
        return
    for name in removable:
        op.drop_column('weather_alerts', name)
