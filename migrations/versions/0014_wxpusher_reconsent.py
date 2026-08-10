"""要求 WxPusher 重新取得独立同意

Revision ID: 0014_wxpusher_reconsent
Revises: 0013_usage_event_privacy
Create Date: 2026-07-18 00:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0014_wxpusher_reconsent'
down_revision = '0013_usage_event_privacy'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if 'users' not in inspect(bind).get_table_names():
        return
    # 历史开启状态没有独立第三方传输同意证据，上线后统一要求重新开启。
    bind.execute(sa.text('UPDATE users SET push_enabled = 0 WHERE push_enabled = 1'))


def downgrade():
    # 无法可靠判断哪些历史账号曾主动同意，降级时继续保持关闭。
    return
