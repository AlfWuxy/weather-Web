"""heat-care collaboration: outcomes, doctor content, devices

Revision ID: 0019_heat_care_collaboration
Revises: 0018_health_consent_care
Create Date: 2026-09-06 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = '0019_heat_care_collaboration'
down_revision = '0018_health_consent_care'
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def _column_names(inspector, table_name):
    if table_name not in inspector.get_table_names():
        return set()
    return {column.get('name') for column in inspector.get_columns(table_name)}


def _index_names(inspector, table_name):
    if table_name not in inspector.get_table_names():
        return set()
    return {item.get('name') for item in inspector.get_indexes() if item.get('name')}


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

    if _table_exists(inspector, 'family_member_profiles'):
        _add_columns(
            'family_member_profiles',
            [
                ('location_query', sa.Column('location_query', sa.String(length=200), nullable=True)),
                (
                    'weather_care_enabled',
                    sa.Column('weather_care_enabled', sa.Boolean(), nullable=False, server_default='0'),
                ),
            ],
            _column_names(inspector, 'family_member_profiles'),
        )

    inspector = inspect(bind)
    if _table_exists(inspector, 'help_requests'):
        _add_columns(
            'help_requests',
            [
                ('assignee_user_id', sa.Column('assignee_user_id', sa.Integer(), nullable=True)),
                ('requested_support_role', sa.Column('requested_support_role', sa.String(length=24), nullable=True)),
                ('proxy_basis', sa.Column('proxy_basis', sa.String(length=120), nullable=True)),
            ],
            _column_names(inspector, 'help_requests'),
        )

    inspector = inspect(bind)
    if not _table_exists(inspector, 'advice_contents'):
        op.create_table(
            'advice_contents',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('public_id', sa.String(length=32), nullable=False),
            sa.Column('kind', sa.String(length=20), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('title', sa.String(length=160), nullable=False),
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('source', sa.String(length=200), nullable=False),
            sa.Column('scenario', sa.String(length=20), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('author_user_id', sa.Integer(), nullable=False),
            sa.Column('reviewer_user_id', sa.Integer(), nullable=True),
            sa.Column('published_at', sa.DateTime(), nullable=True),
            sa.Column('valid_from', sa.DateTime(), nullable=True),
            sa.Column('valid_until', sa.DateTime(), nullable=True),
            sa.Column('parent_id', sa.Integer(), nullable=True),
            sa.Column('is_test', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('public_id'),
        )
        op.create_index('ix_advice_contents_status_kind', 'advice_contents', ['status', 'kind'])
        op.create_index('ix_advice_contents_scenario', 'advice_contents', ['scenario'])

    inspector = inspect(bind)
    if not _table_exists(inspector, 'care_devices'):
        op.create_table(
            'care_devices',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('public_id', sa.String(length=32), nullable=False),
            sa.Column('pair_id', sa.Integer(), nullable=False),
            sa.Column('label', sa.String(length=80), nullable=False, server_default='home-terminal'),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('authorized_at', sa.DateTime(), nullable=False),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('last_seen_at', sa.DateTime(), nullable=True),
            sa.Column('last_heartbeat_at', sa.DateTime(), nullable=True),
            sa.Column('created_by_user_id', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('public_id'),
            sa.UniqueConstraint('token_hash'),
        )
        op.create_index('ix_care_devices_pair_id', 'care_devices', ['pair_id'])
        op.create_index('ix_care_devices_token_hash', 'care_devices', ['token_hash'])

    inspector = inspect(bind)
    if not _table_exists(inspector, 'device_events'):
        op.create_table(
            'device_events',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('device_id', sa.Integer(), nullable=False),
            sa.Column('client_event_id', sa.String(length=64), nullable=False),
            sa.Column('event_name', sa.String(length=40), nullable=False),
            sa.Column('device_occurred_at', sa.DateTime(), nullable=True),
            sa.Column('server_received_at', sa.DateTime(), nullable=False),
            sa.Column('payload_json', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('device_id', 'client_event_id', name='uq_device_events_client_id'),
        )
        op.create_index('ix_device_events_device_id', 'device_events', ['device_id'])
        op.create_index('ix_device_events_event_name', 'device_events', ['event_name'])

    inspector = inspect(bind)
    if _table_exists(inspector, 'pairs'):
        existing_indexes = _index_names(inspector, 'pairs')
        if 'uq_pairs_active_member' not in existing_indexes:
            if bind.dialect.name == 'sqlite':
                op.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pairs_active_member "
                    "ON pairs(member_id) WHERE status = 'active' AND member_id IS NOT NULL"
                ))
            else:
                op.create_index(
                    'uq_pairs_active_member',
                    'pairs',
                    ['member_id'],
                    unique=True,
                    postgresql_where=sa.text("status = 'active' AND member_id IS NOT NULL"),
                )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if _table_exists(inspector, 'device_events'):
        op.drop_table('device_events')
    inspector = inspect(bind)
    if _table_exists(inspector, 'care_devices'):
        op.drop_table('care_devices')
    inspector = inspect(bind)
    if _table_exists(inspector, 'advice_contents'):
        op.drop_table('advice_contents')
