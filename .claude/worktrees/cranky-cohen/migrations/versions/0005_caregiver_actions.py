"""caregiver action log fields

Revision ID: 0005_caregiver_actions
Revises: 0004_short_code_attempts
Create Date: 2025-02-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '0005_caregiver_actions'
down_revision = '0004_short_code_attempts'
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name, column_name):
    try:
        columns = inspector.get_columns(table_name)
    except Exception:
        return False
    return any(column.get('name') == column_name for column in columns)


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if not _table_exists(inspector, 'daily_status'):
        return

    if not _column_exists(inspector, 'daily_status', 'caregiver_actions'):
        op.add_column('daily_status', sa.Column('caregiver_actions', sa.Text()))
    if not _column_exists(inspector, 'daily_status', 'caregiver_note'):
        op.add_column('daily_status', sa.Column('caregiver_note', sa.Text()))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if not _table_exists(inspector, 'daily_status'):
        return

    if _column_exists(inspector, 'daily_status', 'caregiver_note'):
        op.drop_column('daily_status', 'caregiver_note')
    if _column_exists(inspector, 'daily_status', 'caregiver_actions'):
        op.drop_column('daily_status', 'caregiver_actions')
