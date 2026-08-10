"""在降级链启动前保护无法由旧结构表达的复盘

Revision ID: 0021_debrief_downgrade_guard
Revises: 0020_weather_alert_dedupe_key
Create Date: 2026-07-18 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0021_debrief_downgrade_guard'
down_revision = '0020_weather_alert_dedupe_key'
branch_labels = None
depends_on = None


REQUIRED_COLUMNS = {'pair_id', 'owner_user_id', 'origin_pair_id'}


def _count_protected_rows(bind, table_name, column_name, empty_literals):
    """统计旧结构无法表达的非空值，所有调用都发生在任何降级 DDL 前。"""
    conditions = [f'{column_name} IS NOT NULL']
    if empty_literals:
        placeholders = ', '.join(
            f':empty_{index}' for index, _value in enumerate(empty_literals)
        )
        conditions.append(f'TRIM({column_name}) NOT IN ({placeholders})')
    params = {
        f'empty_{index}': value
        for index, value in enumerate(empty_literals)
    }
    return int(
        bind.execute(
            sa.text(
                f'''SELECT COUNT(*) FROM {table_name}
                    WHERE {' AND '.join(conditions)}'''
            ),
            params,
        ).scalar_one()
    )


def upgrade():
    """此 revision 只建立降级前置边界，不改变业务表。"""


def downgrade():
    """在 0020/0019 删列前拒绝所有旧结构无法表达的数据。"""
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    required_tables = {'debriefs', 'daily_status', 'weather_alerts'}
    missing_tables = sorted(required_tables - tables)
    if missing_tables:
        raise RuntimeError(
            'head downgrade preflight aborted: '
            f'missing_tables={missing_tables}; head schema was preserved'
        )

    daily_columns = {
        column['name'] for column in inspector.get_columns('daily_status')
    }
    alert_columns = {
        column['name'] for column in inspector.get_columns('weather_alerts')
    }
    missing_protected_columns = []
    if 'elder_actions' not in daily_columns:
        missing_protected_columns.append('daily_status.elder_actions')
    if 'dedupe_key' not in alert_columns:
        missing_protected_columns.append('weather_alerts.dedupe_key')
    if missing_protected_columns:
        raise RuntimeError(
            'head downgrade preflight aborted: '
            f'missing_columns={missing_protected_columns}; head schema was preserved'
        )

    elder_action_count = _count_protected_rows(
        bind,
        'daily_status',
        'elder_actions',
        ('', '[]', 'null'),
    )
    if elder_action_count:
        raise RuntimeError(
            'head downgrade preflight aborted: '
            f'elder_action_count={elder_action_count}; head schema was preserved'
        )

    dedupe_key_count = _count_protected_rows(
        bind,
        'weather_alerts',
        'dedupe_key',
        ('',),
    )
    if dedupe_key_count:
        raise RuntimeError(
            'head downgrade preflight aborted: '
            f'dedupe_key_count={dedupe_key_count}; head schema was preserved'
        )

    columns = {column['name'] for column in inspector.get_columns('debriefs')}
    missing_columns = REQUIRED_COLUMNS - columns
    if missing_columns:
        raise RuntimeError(
            'debrief downgrade preflight aborted: '
            f'missing_columns={sorted(missing_columns)}; head schema was preserved'
        )

    unrepresentable_count = int(
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
    if unrepresentable_count:
        # Alembic 只有本 revision 成功返回后才会继续删除 0020/0019 字段。
        raise RuntimeError(
            'debrief downgrade preflight aborted: '
            f'unrepresentable_count={unrepresentable_count}; '
            'head schema was preserved'
        )
