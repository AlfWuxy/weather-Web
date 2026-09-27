"""cooling resource verification fields and append-only feedback

Revision ID: 0016_cooling_verification
Revises: 0015_action_events
Create Date: 2026-09-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0016_cooling_verification'
down_revision = '0015_action_events'
branch_labels = None
depends_on = None


NEW_RESOURCE_COLUMNS = (
    ('last_verified_at', sa.Column('last_verified_at', sa.DateTime(), nullable=True)),
    ('verified_by_role', sa.Column('verified_by_role', sa.String(length=16), nullable=True)),
    ('verify_method', sa.Column('verify_method', sa.String(length=16), nullable=True)),
    ('open_during_alert', sa.Column('open_during_alert', sa.String(length=16), nullable=True)),
    (
        'alert_open_note_code',
        sa.Column('alert_open_note_code', sa.String(length=32), nullable=True),
    ),
    ('amenities_json', sa.Column('amenities_json', sa.Text(), nullable=True)),
    ('transport_need', sa.Column('transport_need', sa.String(length=16), nullable=True)),
    ('verify_status', sa.Column('verify_status', sa.String(length=16), nullable=True)),
)


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def _column_names(inspector, table_name):
    if table_name not in inspector.get_table_names():
        return set()
    return {column.get('name') for column in inspector.get_columns(table_name)}


def _index_exists(inspector, table_name, index_name):
    try:
        indexes = inspector.get_indexes(table_name)
    except Exception:
        return False
    return any(idx.get('name') == index_name for idx in indexes)


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


def _drop_columns(table_name, names, existing):
    removable = [name for name in names if name in existing]
    if not removable:
        return
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table(table_name) as batch_op:
            for name in removable:
                batch_op.drop_column(name)
        return
    for name in removable:
        op.drop_column(table_name, name)


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'cooling_resources'):
        _add_columns(
            'cooling_resources',
            NEW_RESOURCE_COLUMNS,
            _column_names(inspector, 'cooling_resources'),
        )

    inspector = inspect(bind)
    if not _table_exists(inspector, 'cooling_feedback'):
        op.create_table(
            'cooling_feedback',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'resource_id',
                sa.Integer(),
                sa.ForeignKey('cooling_resources.id'),
                nullable=False,
            ),
            sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id'), nullable=True),
            sa.Column('code', sa.String(length=16), nullable=False),
            sa.Column('channel', sa.String(length=24), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_cooling_feedback_resource_id', 'cooling_feedback', ['resource_id'])
        op.create_index('ix_cooling_feedback_pair_id', 'cooling_feedback', ['pair_id'])
        op.create_index('ix_cooling_feedback_code', 'cooling_feedback', ['code'])
        op.create_index('ix_cooling_feedback_created_at', 'cooling_feedback', ['created_at'])
        return

    inspector = inspect(bind)
    for index_name, column in (
        ('ix_cooling_feedback_resource_id', ['resource_id']),
        ('ix_cooling_feedback_pair_id', ['pair_id']),
        ('ix_cooling_feedback_code', ['code']),
        ('ix_cooling_feedback_created_at', ['created_at']),
    ):
        if not _index_exists(inspector, 'cooling_feedback', index_name):
            op.create_index(index_name, 'cooling_feedback', column)


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'cooling_feedback'):
        op.drop_table('cooling_feedback')

    inspector = inspect(bind)
    if _table_exists(inspector, 'cooling_resources'):
        _drop_columns(
            'cooling_resources',
            tuple(name for name, _column in NEW_RESOURCE_COLUMNS),
            _column_names(inspector, 'cooling_resources'),
        )
