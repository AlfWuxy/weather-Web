"""schema fixes for explain and audit extra data

Revision ID: 0002_schema_fixes
Revises: 0001_feature_extensions
Create Date: 2025-01-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '0002_schema_fixes'
down_revision = '0001_feature_extensions'
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name, column_name):
    if table_name not in inspector.get_table_names():
        return False
    columns = {column['name'] for column in inspector.get_columns(table_name)}
    return column_name in columns


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'health_risk_assessments'):
        if not _column_exists(inspector, 'health_risk_assessments', 'explain'):
            op.add_column('health_risk_assessments', sa.Column('explain', sa.Text(), nullable=True))

    if _table_exists(inspector, 'audit_logs'):
        has_extra_data = _column_exists(inspector, 'audit_logs', 'extra_data')
        if not has_extra_data:
            op.add_column('audit_logs', sa.Column('extra_data', sa.Text(), nullable=True))
            has_extra_data = True

        if _column_exists(inspector, 'audit_logs', 'metadata') and has_extra_data:
            op.execute(sa.text(
                "UPDATE audit_logs SET extra_data = metadata WHERE extra_data IS NULL"
            ))


def _drop_column_if_exists(table_name, column_name):
    bind = op.get_bind()
    inspector = inspect(bind)
    if not _table_exists(inspector, table_name):
        return
    if not _column_exists(inspector, table_name, column_name):
        return
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_column(column_name)
    else:
        op.drop_column(table_name, column_name)


def downgrade():
    _drop_column_if_exists('audit_logs', 'extra_data')
    _drop_column_if_exists('health_risk_assessments', 'explain')
