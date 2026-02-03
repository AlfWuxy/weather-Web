"""cooling resource fields

Revision ID: 0006_cooling_resource_fields
Revises: 0005_caregiver_actions
Create Date: 2025-02-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '0006_cooling_resource_fields'
down_revision = '0005_caregiver_actions'
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

    if not _table_exists(inspector, 'cooling_resources'):
        return

    if not _column_exists(inspector, 'cooling_resources', 'latitude'):
        op.add_column('cooling_resources', sa.Column('latitude', sa.Float()))
    if not _column_exists(inspector, 'cooling_resources', 'longitude'):
        op.add_column('cooling_resources', sa.Column('longitude', sa.Float()))
    if not _column_exists(inspector, 'cooling_resources', 'has_ac'):
        op.add_column('cooling_resources', sa.Column('has_ac', sa.Boolean()))
    if not _column_exists(inspector, 'cooling_resources', 'is_accessible'):
        op.add_column('cooling_resources', sa.Column('is_accessible', sa.Boolean()))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if not _table_exists(inspector, 'cooling_resources'):
        return

    if _column_exists(inspector, 'cooling_resources', 'is_accessible'):
        op.drop_column('cooling_resources', 'is_accessible')
    if _column_exists(inspector, 'cooling_resources', 'has_ac'):
        op.drop_column('cooling_resources', 'has_ac')
    if _column_exists(inspector, 'cooling_resources', 'longitude'):
        op.drop_column('cooling_resources', 'longitude')
    if _column_exists(inspector, 'cooling_resources', 'latitude'):
        op.drop_column('cooling_resources', 'latitude')
