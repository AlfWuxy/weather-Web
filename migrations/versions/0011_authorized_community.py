"""split authorized_community ACL from user.community location

Revision ID: 0011_authorized_community
Revises: 0010_action_token_hardening
Create Date: 2026-08-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = '0011_authorized_community'
down_revision = '0010_action_token_hardening'
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name, column_name):
    if table_name not in inspector.get_table_names():
        return False
    try:
        cols = inspector.get_columns(table_name)
    except Exception:
        return False
    return any(c.get('name') == column_name for c in cols)


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if not _table_exists(inspector, 'users'):
        return
    if not _column_exists(inspector, 'users', 'authorized_community'):
        op.add_column(
            'users',
            sa.Column('authorized_community', sa.String(length=100), nullable=True),
        )
    # 社区角色：把既有 community 拷到 authorized_community，避免 ACL 空窗
    try:
        bind.execute(
            text(
                "UPDATE users SET authorized_community = community "
                "WHERE role = 'community' "
                "AND (authorized_community IS NULL OR authorized_community = '') "
                "AND community IS NOT NULL AND community != ''"
            )
        )
    except Exception:
        # 兼容不同方言；失败不阻断 upgrade（create_all 路径由应用层兜底）
        pass


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if _table_exists(inspector, 'users') and _column_exists(inspector, 'users', 'authorized_community'):
        op.drop_column('users', 'authorized_community')
