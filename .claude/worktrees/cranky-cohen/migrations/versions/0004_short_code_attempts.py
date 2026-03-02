"""short code attempts

Revision ID: 0004_short_code_attempts
Revises: 0003_action_heat_system
Create Date: 2025-01-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '0004_short_code_attempts'
down_revision = '0003_action_heat_system'
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if not _table_exists(inspector, 'short_code_attempts'):
        op.create_table(
            'short_code_attempts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('key_hash', sa.String(length=64), nullable=False),
            sa.Column('failed_count', sa.Integer()),
            sa.Column('first_failed_at', sa.DateTime()),
            sa.Column('last_failed_at', sa.DateTime()),
            sa.Column('locked_until', sa.DateTime())
        )
        op.create_index('ix_short_code_attempts_key_hash', 'short_code_attempts', ['key_hash'])


def _drop_table_if_exists(table_name):
    bind = op.get_bind()
    inspector = inspect(bind)
    if not _table_exists(inspector, table_name):
        return
    op.drop_table(table_name)


def downgrade():
    _drop_table_if_exists('short_code_attempts')
