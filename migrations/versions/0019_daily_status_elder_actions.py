"""为老人自护行动增加独立字段

Revision ID: 0019_daily_status_elder_actions
Revises: 0018_debrief_owner_scope
Create Date: 2026-07-18 11:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0019_daily_status_elder_actions'
down_revision = '0018_debrief_owner_scope'
branch_labels = None
depends_on = None


def _has_column(inspector, table_name, column_name):
    return any(
        column.get('name') == column_name
        for column in inspector.get_columns(table_name)
    )


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'daily_status' not in inspector.get_table_names():
        return
    if not _has_column(inspector, 'daily_status', 'elder_actions'):
        op.add_column(
            'daily_status',
            sa.Column('elder_actions', sa.Text(), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'daily_status' not in inspector.get_table_names():
        return
    if _has_column(inspector, 'daily_status', 'elder_actions'):
        protected_count = int(
            bind.execute(
                sa.text(
                    '''
                    SELECT COUNT(*)
                    FROM daily_status
                    WHERE elder_actions IS NOT NULL
                      AND TRIM(elder_actions) NOT IN ('', '[]', 'null')
                    '''
                )
            ).scalar_one()
        )
        if protected_count:
            raise RuntimeError(
                'elder actions downgrade aborted: '
                f'protected_count={protected_count}; elder_actions was preserved'
            )
        op.drop_column('daily_status', 'elder_actions')
