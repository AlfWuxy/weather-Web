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


def _preflight_downgrade(bind, inspector):
    """旧结构无法表达独立授权，任何有效授权都必须先由人工撤销。"""
    columns = _user_columns(inspector)
    missing = sorted({'role', COLUMN_NAME} - set(columns))
    if missing:
        raise RuntimeError(
            'authorized community downgrade aborted: '
            f'missing_columns={missing}; schema and rows were preserved'
        )

    operating_role_count = int(bind.execute(sa.text(
        "SELECT COUNT(*) FROM users "
        "WHERE role IN ('community', 'caregiver')"
    )).scalar_one())
    if operating_role_count:
        raise RuntimeError(
            'authorized community downgrade aborted: '
            f'operating_role_count={operating_role_count}; '
            'schema and rows were preserved'
        )

    authorized_community_count = int(bind.execute(sa.text(
        '''SELECT COUNT(*) FROM users
           WHERE authorized_community IS NOT NULL
             AND TRIM(authorized_community) != '' '''
    )).scalar_one())
    if authorized_community_count:
        raise RuntimeError(
            'authorized community downgrade aborted: '
            f'authorized_community_count={authorized_community_count}; '
            'schema and rows were preserved'
        )


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

    # SQLite DDL 不可回滚，丢数据检查必须位于首个写操作之前。
    _preflight_downgrade(bind, inspector)
    op.drop_column('users', COLUMN_NAME)
