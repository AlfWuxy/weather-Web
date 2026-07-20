"""固化微信小程序首次登录来源

Revision ID: 0015_miniprogram_acquisition
Revises: 0014_wxpusher_reconsent
Create Date: 2026-07-18 01:00:00.000000
"""

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0015_miniprogram_acquisition'
down_revision = '0014_wxpusher_reconsent'
branch_labels = None
depends_on = None


def _column_exists(inspector, table_name, column_name):
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column.get('name') == column_name
        for column in inspector.get_columns(table_name)
    )


def _first_login_sources(bind):
    """从仍在保留期内的最早登录事件尽量恢复首次来源。"""
    rows = bind.execute(
        sa.text(
            '''
            SELECT user_id, meta_json
            FROM usage_events
            WHERE user_id IS NOT NULL
              AND event_type = 'wechat_login_success'
              AND source = 'miniprogram'
            ORDER BY created_at ASC, id ASC
            '''
        )
    )
    sources = {}
    for row in rows:
        user_id = int(row.user_id)
        if user_id in sources:
            continue
        try:
            meta = json.loads(row.meta_json or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            meta = {}
        source = meta.get('from') if isinstance(meta, dict) else None
        if source in {'direct', 'family_share'}:
            sources[user_id] = source
    return sources


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'miniprogram_identities' not in inspector.get_table_names():
        return
    if not _column_exists(inspector, 'miniprogram_identities', 'acquisition_source'):
        op.add_column(
            'miniprogram_identities',
            sa.Column(
                'acquisition_source',
                sa.String(length=20),
                nullable=False,
                # 旧历史若已在前序隐私迁移中去除来源证据，必须排除而非猜成 direct。
                server_default='unknown',
            ),
        )

    if 'usage_events' in inspector.get_table_names():
        for user_id, source in _first_login_sources(bind).items():
            bind.execute(
                sa.text(
                    '''
                    UPDATE miniprogram_identities
                    SET acquisition_source = :source
                    WHERE user_id = :user_id
                    '''
                ),
                {'source': source, 'user_id': user_id},
            )

    inspector = inspect(bind)
    index_names = {
        index.get('name')
        for index in inspector.get_indexes('miniprogram_identities')
    }
    if 'ix_miniprogram_identities_created_at' not in index_names:
        op.create_index(
            'ix_miniprogram_identities_created_at',
            'miniprogram_identities',
            ['created_at'],
            unique=False,
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if not _column_exists(inspector, 'miniprogram_identities', 'acquisition_source'):
        return
    index_names = {
        index.get('name')
        for index in inspector.get_indexes('miniprogram_identities')
    }
    if 'ix_miniprogram_identities_created_at' in index_names:
        op.drop_index(
            'ix_miniprogram_identities_created_at',
            table_name='miniprogram_identities',
        )
    op.drop_column('miniprogram_identities', 'acquisition_source')
