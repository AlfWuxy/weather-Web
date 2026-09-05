"""health consent columns and assessment member_id

Revision ID: 0018_health_consent_care
Revises: 0017_family_help_outbox
Create Date: 2026-09-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0018_health_consent_care'
down_revision = '0017_family_help_outbox'
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def _column_names(inspector, table_name):
    if table_name not in inspector.get_table_names():
        return set()
    return {column.get('name') for column in inspector.get_columns(table_name)}


def _add_columns(table_name, columns, existing):
    missing = [(name, column) for name, column in columns if name not in existing]
    if not missing:
        return
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table(table_name) as batch_op:
            for _name, column in missing:
                batch_op.add_column(column)
        return
    for _name, column in missing:
        op.add_column(table_name, column)


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if _table_exists(inspector, 'users'):
        _add_columns(
            'users',
            [
                ('health_sensitive_consent_version', sa.Column('health_sensitive_consent_version', sa.String(length=64), nullable=True)),
                ('health_sensitive_consented_at', sa.Column('health_sensitive_consented_at', sa.DateTime(), nullable=True)),
            ],
            _column_names(inspector, 'users'),
        )
    inspector = inspect(bind)
    if _table_exists(inspector, 'health_risk_assessments'):
        _add_columns(
            'health_risk_assessments',
            [('member_id', sa.Column('member_id', sa.Integer(), nullable=True))],
            _column_names(inspector, 'health_risk_assessments'),
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if _table_exists(inspector, 'health_risk_assessments') and 'member_id' in _column_names(inspector, 'health_risk_assessments'):
        with op.batch_alter_table('health_risk_assessments') as batch_op:
            batch_op.drop_column('member_id')
    inspector = inspect(bind)
    if _table_exists(inspector, 'users'):
        existing = _column_names(inspector, 'users')
        with op.batch_alter_table('users') as batch_op:
            if 'health_sensitive_consented_at' in existing:
                batch_op.drop_column('health_sensitive_consented_at')
            if 'health_sensitive_consent_version' in existing:
                batch_op.drop_column('health_sensitive_consent_version')
