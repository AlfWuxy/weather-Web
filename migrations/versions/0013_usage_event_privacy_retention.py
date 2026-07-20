"""埋点家庭标识清理与保留策略索引

Revision ID: 0013_usage_event_privacy
Revises: 0012_repair_usage_event_fks
Create Date: 2026-07-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0013_usage_event_privacy'
down_revision = '0012_repair_usage_event_fks'
branch_labels = None
depends_on = None

_RETENTION_INDEX_NAME = 'ix_usage_events_retention_created_at'


def _table_exists(inspector, name):
    return name in inspector.get_table_names()


def _has_created_at_index(inspector):
    return any(
        tuple(index.get('column_names') or ()) == ('created_at',)
        for index in inspector.get_indexes('usage_events')
    )


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if not _table_exists(inspector, 'usage_events'):
        return

    # 历史事件同步去除家庭和成员粒度；新写入路径也会始终留空。
    bind.execute(
        sa.text(
            '''
            UPDATE usage_events
            SET pair_id = NULL, member_id = NULL
            WHERE pair_id IS NOT NULL OR member_id IS NOT NULL
            '''
        )
    )

    # 30 天清理按 created_at 扫描；仅在旧库缺少等价索引时补建。
    if not _has_created_at_index(inspector):
        op.create_index(
            _RETENTION_INDEX_NAME,
            'usage_events',
            ['created_at'],
            unique=False,
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if not _table_exists(inspector, 'usage_events'):
        return
    index_names = {
        index.get('name')
        for index in inspector.get_indexes('usage_events')
    }
    if _RETENTION_INDEX_NAME in index_names:
        op.drop_index(_RETENTION_INDEX_NAME, table_name='usage_events')
    # 已清除的家庭与成员标识无法可靠恢复，降级继续保留去标识历史事件。
