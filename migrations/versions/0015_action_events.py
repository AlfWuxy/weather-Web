"""action events append-only chain and daily summary columns

Revision ID: 0015_action_events
Revises: 0014_weather_alert_provenance
Create Date: 2026-09-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0015_action_events'
down_revision = '0014_weather_alert_provenance'
branch_labels = None
depends_on = None


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
            for name, column in missing:
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

    if _table_exists(inspector, 'pairs'):
        pair_columns = _column_names(inspector, 'pairs')
        _add_columns(
            'pairs',
            [
                (
                    'is_test',
                    sa.Column(
                        'is_test',
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.false(),
                    ),
                )
            ],
            pair_columns,
        )

    if _table_exists(inspector, 'daily_status'):
        status_columns = _column_names(inspector, 'daily_status')
        _add_columns(
            'daily_status',
            [
                ('understood_at', sa.Column('understood_at', sa.DateTime(), nullable=True)),
                ('verified_at', sa.Column('verified_at', sa.DateTime(), nullable=True)),
                (
                    'help_acknowledged_at',
                    sa.Column('help_acknowledged_at', sa.DateTime(), nullable=True),
                ),
                ('closed_at', sa.Column('closed_at', sa.DateTime(), nullable=True)),
            ],
            status_columns,
        )

    if _table_exists(inspector, 'community_daily'):
        community_columns = _column_names(inspector, 'community_daily')
        _add_columns(
            'community_daily',
            [
                ('understood_rate', sa.Column('understood_rate', sa.Float(), nullable=True)),
                ('self_report_rate', sa.Column('self_report_rate', sa.Float(), nullable=True)),
                ('verified_rate', sa.Column('verified_rate', sa.Float(), nullable=True)),
                ('open_help_count', sa.Column('open_help_count', sa.Integer(), nullable=True)),
                ('unknown_count', sa.Column('unknown_count', sa.Integer(), nullable=True)),
            ],
            community_columns,
        )

    if not _table_exists(inspector, 'action_events'):
        op.create_table(
            'action_events',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id'), nullable=False),
            sa.Column('local_date', sa.Date(), nullable=False),
            sa.Column('stage', sa.String(length=32), nullable=False),
            sa.Column('actor_role', sa.String(length=16), nullable=False),
            sa.Column('channel', sa.String(length=24), nullable=False),
            sa.Column('script_version', sa.String(length=16), nullable=True),
            sa.Column('action_id', sa.String(length=32), nullable=True),
            sa.Column('alert_id', sa.Integer(), sa.ForeignKey('weather_alerts.id'), nullable=True),
            sa.Column('delivery_id', sa.Integer(), sa.ForeignKey('alert_deliveries.id'), nullable=True),
            sa.Column('meta_json', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_action_events_pair_id', 'action_events', ['pair_id'])
        op.create_index('ix_action_events_local_date', 'action_events', ['local_date'])
        op.create_index('ix_action_events_stage', 'action_events', ['stage'])
        op.create_index('ix_action_events_created_at', 'action_events', ['created_at'])
        op.create_index(
            'ix_action_events_pair_date_stage',
            'action_events',
            ['pair_id', 'local_date', 'stage'],
        )
        return

    inspector = inspect(bind)
    if not _index_exists(inspector, 'action_events', 'ix_action_events_pair_date_stage'):
        op.create_index(
            'ix_action_events_pair_date_stage',
            'action_events',
            ['pair_id', 'local_date', 'stage'],
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'action_events'):
        op.drop_table('action_events')

    inspector = inspect(bind)
    if _table_exists(inspector, 'community_daily'):
        _drop_columns(
            'community_daily',
            (
                'unknown_count',
                'open_help_count',
                'verified_rate',
                'self_report_rate',
                'understood_rate',
            ),
            _column_names(inspector, 'community_daily'),
        )

    inspector = inspect(bind)
    if _table_exists(inspector, 'daily_status'):
        _drop_columns(
            'daily_status',
            ('closed_at', 'help_acknowledged_at', 'verified_at', 'understood_at'),
            _column_names(inspector, 'daily_status'),
        )

    inspector = inspect(bind)
    if _table_exists(inspector, 'pairs'):
        _drop_columns('pairs', ('is_test',), _column_names(inspector, 'pairs'))
