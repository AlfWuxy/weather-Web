"""增加天气实况来源与可信度字段

Revision ID: 0030_weather_data_provenance
Revises: 0029_wxpusher_uid_ownership
Create Date: 2026-08-28 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0030_weather_data_provenance'
down_revision = '0029_wxpusher_uid_ownership'
branch_labels = None
depends_on = None


COLUMN_SPECS = {
    'data_source': (sa.String, 32, True, None),
    'observed_at': (sa.DateTime, None, True, None),
    'quality_version': (sa.Integer, None, False, '0'),
    'air_quality_available': (sa.Boolean, None, False, '0'),
}
BASE_COLUMN_SPECS = {
    'id': (sa.Integer, None, False),
    'date': (sa.Date, None, False),
    'location': (sa.String, 100, False),
}
UNIQUE_NAME = 'uq_weather_data_date_location'


def _begin_sqlite_write_transaction(bind):
    """SQLite 首个 DDL 前开启真实写事务，确保中途失败可整体回滚。"""
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
    tables = set(inspector.get_table_names())
    if 'weather_data' not in tables:
        raise RuntimeError(
            "weather provenance migration aborted: missing_tables=['weather_data']"
        )
    return {
        column['name']: column
        for column in inspector.get_columns('weather_data')
    }


def _index_has_predicate(index):
    for key, value in (index.get('dialect_options') or {}).items():
        if key.endswith('_where') and value is not None and str(value).strip():
            return True
    return False


def _baseline_default_is_valid(name, column):
    """PostgreSQL SERIAL/IDENTITY 主键允许数据库生成的序列默认值。"""
    raw_default = str(column.get('default') or '').strip()
    if not raw_default:
        return True
    if name != 'id':
        return False
    if column.get('identity'):
        return True
    return raw_default.lower().startswith('nextval(')


def _datetime_timezone_is_valid(column, dialect_name):
    if str(dialect_name or '').lower() != 'postgresql':
        return True
    return getattr(column.get('type'), 'timezone', False) is True


def _validate_weather_data_baseline(inspector, columns):
    """确认 0029 的天气表基线完整，防止错误 stamp 后继续写 DDL。"""
    invalid = False
    for name, (expected_type, expected_length, nullable) in BASE_COLUMN_SPECS.items():
        column = columns.get(name)
        if column is None:
            invalid = True
            continue
        column_type = column.get('type')
        if not isinstance(column_type, expected_type):
            invalid = True
        if expected_length is not None:
            invalid = invalid or getattr(column_type, 'length', None) != expected_length
        if column.get('nullable') is not nullable:
            invalid = True
        if not _baseline_default_is_valid(name, column):
            invalid = True
    id_column = columns.get('id')
    if id_column is not None and not bool(id_column.get('primary_key')):
        invalid = True

    named_constraints = [
        item
        for item in inspector.get_unique_constraints('weather_data')
        if item.get('name') == UNIQUE_NAME
    ]
    named_indexes = [
        item
        for item in inspector.get_indexes('weather_data')
        if item.get('name') == UNIQUE_NAME
    ]
    # PostgreSQL 会同时反射 UNIQUE constraint 与其 backing index，后者带
    # duplicates_constraint，二者属于同一个语义对象。
    named_objects = named_constraints + [
        item
        for item in named_indexes
        if not (
            named_constraints
            and item.get('duplicates_constraint') == UNIQUE_NAME
        )
    ]
    if len(named_objects) != 1:
        invalid = True
    elif (
        list(named_objects[0].get('column_names') or []) != ['date', 'location']
        or (
            named_objects[0] in named_indexes
            and not bool(named_objects[0].get('unique'))
        )
        or _index_has_predicate(named_objects[0])
    ):
        invalid = True

    if invalid:
        raise RuntimeError(
            'weather provenance migration aborted: invalid_weather_data_baseline'
        )


def _validate_existing(columns, dialect_name=None):
    """同名列必须完全兼容，禁止把未知半迁移结构继续向前推进。"""
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
                'weather provenance migration aborted: '
                f'invalid_column={name}'
            )


def _protected_row_count(bind, columns=None):
    """只引用实际存在的 provenance 列，兼容受控 partial schema 回滚。"""
    existing = set(columns or {
        column['name']
        for column in inspect(bind).get_columns('weather_data')
    })
    definitions = {
        'data_source': sa.String,
        'observed_at': sa.DateTime,
        'quality_version': sa.Integer,
        'air_quality_available': sa.Boolean,
    }
    present = [name for name in definitions if name in existing]
    if not present:
        return 0
    table = sa.table(
        'weather_data',
        *(sa.column(name, definitions[name]) for name in present),
    )
    conditions = []
    if 'data_source' in existing:
        conditions.append(table.c.data_source.is_not(None))
    if 'observed_at' in existing:
        conditions.append(table.c.observed_at.is_not(None))
    if 'quality_version' in existing:
        conditions.append(table.c.quality_version != 0)
    if 'air_quality_available' in existing:
        conditions.append(table.c.air_quality_available.is_(True))
    return int(bind.execute(
        sa.select(sa.func.count()).select_from(table).where(sa.or_(*conditions))
    ).scalar_one())


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = _columns(inspector)
    _validate_weather_data_baseline(inspector, columns)
    _validate_existing(columns, bind.dialect.name)
    missing = [name for name in COLUMN_SPECS if name not in columns]
    if not missing:
        return

    _begin_sqlite_write_transaction(bind)
    # 历史行不回填来源或观测时刻；0/false 明确保持 fail-closed。
    if 'data_source' in missing:
        op.add_column(
            'weather_data',
            sa.Column('data_source', sa.String(length=32), nullable=True),
        )
    if 'observed_at' in missing:
        op.add_column(
            'weather_data',
            sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        )
    if 'quality_version' in missing:
        op.add_column(
            'weather_data',
            sa.Column(
                'quality_version',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            ),
        )
    if 'air_quality_available' in missing:
        op.add_column(
            'weather_data',
            sa.Column(
                'air_quality_available',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = _columns(inspector)
    _validate_weather_data_baseline(inspector, columns)
    _validate_existing(columns, bind.dialect.name)
    removable = [name for name in reversed(COLUMN_SPECS) if name in columns]
    if not removable:
        return
    protected_count = _protected_row_count(bind, columns)
    if protected_count:
        raise RuntimeError(
            'weather provenance downgrade aborted: '
            f'protected_count={protected_count}; provenance columns were preserved'
        )

    _begin_sqlite_write_transaction(bind)
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table('weather_data') as batch_op:
            for name in removable:
                batch_op.drop_column(name)
        return
    for name in removable:
        op.drop_column('weather_data', name)
