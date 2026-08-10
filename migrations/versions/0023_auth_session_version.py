"""为全端会话增加可撤销认证版本

Revision ID: 0023_auth_session_version
Revises: 0022_private_health_indexes
Create Date: 2026-07-18 18:45:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0023_auth_session_version'
down_revision = '0022_private_health_indexes'
branch_labels = None
depends_on = None


REQUIRED_TABLES = {
    'users',
    'pairs',
    'debriefs',
    'daily_status',
    'weather_alerts',
}


def _columns(inspector, table_name):
    return {
        column['name']
        for column in inspector.get_columns(table_name)
    }


def _count_nonempty_text(bind, table_name, column_name, empty_literals):
    """统计旧结构无法表达的非空文本值。"""
    placeholders = ', '.join(
        f':empty_{index}' for index, _value in enumerate(empty_literals)
    )
    params = {
        f'empty_{index}': value
        for index, value in enumerate(empty_literals)
    }
    return int(
        bind.execute(
            sa.text(
                f'''SELECT COUNT(*) FROM {table_name}
                    WHERE {column_name} IS NOT NULL
                      AND TRIM({column_name}) NOT IN ({placeholders})'''
            ),
            params,
        ).scalar_one()
    )


def _preflight_downgrade(bind, inspector):
    """在任何 DDL 前确认整条 head 降级链不会丢失业务语义。"""
    tables = set(inspector.get_table_names())
    missing_tables = sorted(REQUIRED_TABLES - tables)
    if missing_tables:
        raise RuntimeError(
            'auth session downgrade preflight aborted: '
            f'missing_tables={missing_tables}; head schema was preserved'
        )

    required_columns = {
        'users': {'auth_version'},
        'daily_status': {'elder_actions'},
        'weather_alerts': {'dedupe_key'},
        'debriefs': {'pair_id', 'owner_user_id', 'origin_pair_id'},
        'pairs': {'id', 'caregiver_id'},
    }
    missing_columns = []
    for table_name, expected_columns in required_columns.items():
        available_columns = _columns(inspector, table_name)
        missing_columns.extend(
            f'{table_name}.{column_name}'
            for column_name in sorted(expected_columns - available_columns)
        )
    if missing_columns:
        raise RuntimeError(
            'auth session downgrade preflight aborted: '
            f'missing_columns={missing_columns}; head schema was preserved'
        )

    auth_version_count = int(
        bind.execute(
            sa.text(
                '''SELECT COUNT(*) FROM users
                   WHERE auth_version IS NULL OR auth_version != 1'''
            )
        ).scalar_one()
    )
    if auth_version_count:
        raise RuntimeError(
            'auth session downgrade preflight aborted: '
            f'auth_version_count={auth_version_count}; head schema was preserved'
        )

    elder_action_count = _count_nonempty_text(
        bind,
        'daily_status',
        'elder_actions',
        ('', '[]', 'null'),
    )
    if elder_action_count:
        raise RuntimeError(
            'auth session downgrade preflight aborted: '
            f'elder_action_count={elder_action_count}; head schema was preserved'
        )

    dedupe_key_count = _count_nonempty_text(
        bind,
        'weather_alerts',
        'dedupe_key',
        ('',),
    )
    if dedupe_key_count:
        raise RuntimeError(
            'auth session downgrade preflight aborted: '
            f'dedupe_key_count={dedupe_key_count}; head schema was preserved'
        )

    unrepresentable_debrief_count = int(
        bind.execute(
            sa.text(
                '''
                SELECT COUNT(*)
                FROM debriefs AS debrief
                LEFT JOIN users AS owner ON owner.id = debrief.owner_user_id
                LEFT JOIN pairs AS origin_pair
                  ON origin_pair.id = debrief.origin_pair_id
                LEFT JOIN pairs AS display_pair
                  ON display_pair.id = debrief.pair_id
                WHERE debrief.pair_id IS NULL
                   OR display_pair.id IS NULL
                   OR debrief.owner_user_id IS NULL
                   OR owner.id IS NULL
                   OR debrief.origin_pair_id IS NULL
                   OR origin_pair.id IS NULL
                   OR debrief.origin_pair_id != debrief.pair_id
                   OR origin_pair.caregiver_id != debrief.owner_user_id
                   OR display_pair.caregiver_id != debrief.owner_user_id
                '''
            )
        ).scalar_one()
    )
    if unrepresentable_debrief_count:
        raise RuntimeError(
            'auth session downgrade preflight aborted: '
            'unrepresentable_debrief_count='
            f'{unrepresentable_debrief_count}; head schema was preserved'
        )


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'users' not in inspector.get_table_names():
        raise RuntimeError(
            'auth session migration aborted: missing_tables=[\'users\']'
        )
    user_columns = {
        column['name']: column
        for column in inspector.get_columns('users')
    }
    existing_column = user_columns.get('auth_version')
    if existing_column is not None:
        column_type = existing_column.get('type')
        invalid_shape = (
            existing_column.get('nullable') is not False
            or not isinstance(column_type, sa.Integer)
        )
        if invalid_shape:
            raise RuntimeError(
                'auth session migration aborted: '
                'auth_version_invalid_schema; revision was not advanced'
            )
        invalid_value_count = int(
            bind.execute(
                sa.text(
                    '''SELECT COUNT(*) FROM users
                       WHERE auth_version IS NULL OR auth_version < 1'''
                )
            ).scalar_one()
        )
        if invalid_value_count:
            raise RuntimeError(
                'auth session migration aborted: '
                f'auth_version_invalid_value_count={invalid_value_count}; '
                'revision was not advanced'
            )
        return
    op.add_column(
        'users',
        sa.Column(
            'auth_version',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('1'),
        ),
    )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    _preflight_downgrade(bind, inspector)
    op.drop_column('users', 'auth_version')
