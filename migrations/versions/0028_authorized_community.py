"""增加独立运营社区授权并默认拒绝历史空映射

Revision ID: 0028_authorized_community
Revises: 0027_cross_platform_identity
Create Date: 2026-08-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0028_authorized_community'
down_revision = '0027_cross_platform_identity'
branch_labels = None
depends_on = None


COLUMN_NAME = 'authorized_community'


def _user_columns(inspector):
    return {
        column['name']: column
        for column in inspector.get_columns('users')
    }


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'users' not in inspector.get_table_names():
        raise RuntimeError(
            "authorized community migration aborted: missing_tables=['users']"
        )

    columns = _user_columns(inspector)
    existing = columns.get(COLUMN_NAME)
    if existing is not None:
        column_type = existing.get('type')
        if (
            not isinstance(column_type, sa.String)
            or getattr(column_type, 'length', None) != 100
            or existing.get('nullable') is not True
        ):
            raise RuntimeError(
                'authorized community migration aborted: invalid users column'
            )
        return

    op.add_column(
        'users',
        sa.Column(COLUMN_NAME, sa.String(length=100), nullable=True),
    )
    # 历史 community 曾由用户自行修改，不能自动提升为运营授权。
    bind.execute(sa.text(
        'UPDATE users SET authorized_community = NULL'
    ))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'users' not in inspector.get_table_names():
        return
    if COLUMN_NAME not in _user_columns(inspector):
        return

    # 旧代码会把定位字段当 ACL。回滚前先撤销受影响运营角色，保持安全失败。
    bind.execute(sa.text(
        "UPDATE users SET role = 'user' "
        "WHERE role IN ('community', 'caregiver')"
    ))
    op.drop_column('users', COLUMN_NAME)
