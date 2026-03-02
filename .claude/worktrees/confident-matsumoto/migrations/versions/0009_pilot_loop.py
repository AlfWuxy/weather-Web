"""pilot loop tables (api tokens / usage events / push deliveries / location cache)

Revision ID: 0009_pilot_loop
Revises: 0008_weather_uniques
Create Date: 2026-02-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '0009_pilot_loop'
down_revision = '0008_weather_uniques'
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name, column_name):
    if table_name not in inspector.get_table_names():
        return False
    try:
        cols = inspector.get_columns(table_name)
    except Exception:
        return False
    return any(col.get('name') == column_name for col in cols)


def _index_exists(inspector, table_name, index_name):
    try:
        indexes = inspector.get_indexes(table_name)
    except Exception:
        return False
    return any(idx.get('name') == index_name for idx in indexes)


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if not _table_exists(inspector, 'api_tokens'):
        op.create_table(
            'api_tokens',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('name', sa.String(length=80), nullable=True),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_api_tokens_user_id', 'api_tokens', ['user_id'])
        op.create_index('ix_api_tokens_token_hash', 'api_tokens', ['token_hash'])

    if not _table_exists(inspector, 'usage_events'):
        op.create_table(
            'usage_events',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id'), nullable=True),
            sa.Column('member_id', sa.Integer(), sa.ForeignKey('family_members.id'), nullable=True),
            sa.Column('event_type', sa.String(length=50), nullable=False),
            sa.Column('meta_json', sa.Text(), nullable=True),
            sa.Column('source', sa.String(length=20), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_usage_events_user_id', 'usage_events', ['user_id'])
        op.create_index('ix_usage_events_event_type', 'usage_events', ['event_type'])
        op.create_index('ix_usage_events_created_at', 'usage_events', ['created_at'])

    if not _table_exists(inspector, 'alert_deliveries'):
        op.create_table(
            'alert_deliveries',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('alert_id', sa.Integer(), sa.ForeignKey('weather_alerts.id'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id'), nullable=True),
            sa.Column('channel', sa.String(length=20), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=True),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('delivery_token', sa.String(length=64), nullable=False),
            sa.Column('sent_at', sa.DateTime(), nullable=True),
            sa.Column('clicked_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_alert_deliveries_alert_user', 'alert_deliveries', ['alert_id', 'user_id'])
        op.create_index('ix_alert_deliveries_delivery_token', 'alert_deliveries', ['delivery_token'], unique=True)

    if not _table_exists(inspector, 'location_cache'):
        op.create_table(
            'location_cache',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('query', sa.String(length=200), nullable=False),
            sa.Column('location_code', sa.String(length=100), nullable=False),
            sa.Column('provider', sa.String(length=20), nullable=True),
            sa.Column('raw_json', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_location_cache_query', 'location_cache', ['query'])
        op.create_index('ix_location_cache_updated_at', 'location_cache', ['updated_at'])

    # Extend pairs for pilot loop (link to elder profile + free-form location query)
    if _table_exists(inspector, 'pairs'):
        if not _column_exists(inspector, 'pairs', 'member_id'):
            # SQLite cannot add FK constraints post-hoc; store as integer.
            op.add_column('pairs', sa.Column('member_id', sa.Integer()))
        if not _column_exists(inspector, 'pairs', 'location_query'):
            op.add_column('pairs', sa.Column('location_query', sa.String(length=200)))
        if not _index_exists(inspector, 'pairs', 'ix_pairs_member_id'):
            op.create_index('ix_pairs_member_id', 'pairs', ['member_id'])

    # Extend users for WxPusher push settings
    if _table_exists(inspector, 'users'):
        if not _column_exists(inspector, 'users', 'wxpusher_uid'):
            op.add_column('users', sa.Column('wxpusher_uid', sa.String(length=80)))
        if not _column_exists(inspector, 'users', 'push_enabled'):
            op.add_column(
                'users',
                sa.Column('push_enabled', sa.Boolean(), nullable=True, server_default=sa.false())
            )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    # Best-effort downgrade; SQLite column drops are skipped to avoid destructive operations.
    if _table_exists(inspector, 'location_cache'):
        op.drop_table('location_cache')
    if _table_exists(inspector, 'alert_deliveries'):
        op.drop_table('alert_deliveries')
    if _table_exists(inspector, 'usage_events'):
        op.drop_table('usage_events')
    if _table_exists(inspector, 'api_tokens'):
        op.drop_table('api_tokens')

