"""family spaces, help requests, notification outbox, idempotency keys

Revision ID: 0017_family_help_outbox
Revises: 0016_cooling_verification
Create Date: 2026-09-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0017_family_help_outbox'
down_revision = '0016_cooling_verification'
branch_labels = None
depends_on = None

OPEN_HELP_WHERE = "status IN ('pending_ack', 'acknowledged', 'in_progress')"


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


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if not _table_exists(inspector, 'family_spaces'):
        op.create_table(
            'family_spaces',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('public_id', sa.String(length=32), nullable=False),
            sa.Column('name', sa.String(length=80), nullable=False),
            sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('is_test', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('created_at', sa.DateTime()),
            sa.UniqueConstraint('public_id', name='uq_family_spaces_public_id'),
        )
        op.create_index('ix_family_spaces_created_by', 'family_spaces', ['created_by_user_id'])

    inspector = inspect(bind)
    if not _table_exists(inspector, 'family_memberships'):
        op.create_table(
            'family_memberships',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('family_space_id', sa.Integer(), sa.ForeignKey('family_spaces.id'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('role', sa.String(length=32), nullable=False),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('invited_by_user_id', sa.Integer(), sa.ForeignKey('users.id')),
            sa.Column('created_at', sa.DateTime()),
            sa.Column('revoked_at', sa.DateTime()),
        )
        op.create_index('ix_family_memberships_user_id', 'family_memberships', ['user_id'])
        op.create_index('ix_family_memberships_space_id', 'family_memberships', ['family_space_id'])
        op.create_index(
            'uq_family_memberships_active_user',
            'family_memberships',
            ['family_space_id', 'user_id'],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )

    inspector = inspect(bind)
    if not _table_exists(inspector, 'family_invites'):
        op.create_table(
            'family_invites',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('family_space_id', sa.Integer(), sa.ForeignKey('family_spaces.id'), nullable=False),
            sa.Column('code_hash', sa.String(length=64), nullable=False),
            sa.Column('role', sa.String(length=32), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('max_uses', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('use_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('revoked_at', sa.DateTime()),
            sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('created_at', sa.DateTime()),
            sa.Column('last_consumed_at', sa.DateTime()),
            sa.UniqueConstraint('code_hash', name='uq_family_invites_code_hash'),
        )
        op.create_index('ix_family_invites_space_id', 'family_invites', ['family_space_id'])
        op.create_index('ix_family_invites_expires_at', 'family_invites', ['expires_at'])

    inspector = inspect(bind)
    if _table_exists(inspector, 'users'):
        _add_columns(
            'users',
            [('deleted_at', sa.Column('deleted_at', sa.DateTime(), nullable=True))],
            _column_names(inspector, 'users'),
        )

    inspector = inspect(bind)
    if _table_exists(inspector, 'pairs'):
        _add_columns(
            'pairs',
            [('family_space_id', sa.Column('family_space_id', sa.Integer(), nullable=True))],
            _column_names(inspector, 'pairs'),
        )
        inspector = inspect(bind)
        if not _index_exists(inspector, 'pairs', 'ix_pairs_family_space_id'):
            op.create_index('ix_pairs_family_space_id', 'pairs', ['family_space_id'])

    inspector = inspect(bind)
    if not _table_exists(inspector, 'help_requests'):
        op.create_table(
            'help_requests',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('public_id', sa.String(length=32), nullable=False),
            sa.Column('family_space_id', sa.Integer(), sa.ForeignKey('family_spaces.id'), nullable=False),
            sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id'), nullable=False),
            sa.Column('status', sa.String(length=24), nullable=False),
            sa.Column('origin_channel', sa.String(length=24), nullable=False),
            sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id')),
            sa.Column('actor_role', sa.String(length=24), nullable=False),
            sa.Column('is_proxy', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('category', sa.String(length=32), nullable=False, server_default='cannot_complete'),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('acknowledged_by_user_id', sa.Integer(), sa.ForeignKey('users.id')),
            sa.Column('acknowledged_at', sa.DateTime()),
            sa.Column('started_by_user_id', sa.Integer(), sa.ForeignKey('users.id')),
            sa.Column('started_at', sa.DateTime()),
            sa.Column('resolved_by_user_id', sa.Integer(), sa.ForeignKey('users.id')),
            sa.Column('resolved_at', sa.DateTime()),
            sa.Column('resolution_code', sa.String(length=32)),
            sa.Column('cancelled_by_user_id', sa.Integer(), sa.ForeignKey('users.id')),
            sa.Column('cancelled_at', sa.DateTime()),
            sa.Column('cancel_reason_code', sa.String(length=32)),
            sa.Column('is_test', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('legacy_source', sa.String(length=32)),
            sa.Column('created_at', sa.DateTime()),
            sa.Column('updated_at', sa.DateTime()),
            sa.UniqueConstraint('public_id', name='uq_help_requests_public_id'),
        )
        op.create_index('ix_help_requests_pair_id', 'help_requests', ['pair_id'])
        op.create_index('ix_help_requests_space_status', 'help_requests', ['family_space_id', 'status'])
        op.create_index('ix_help_requests_updated_at', 'help_requests', ['updated_at'])
        op.create_index(
            'uq_help_requests_open_pair',
            'help_requests',
            ['pair_id'],
            unique=True,
            sqlite_where=sa.text(OPEN_HELP_WHERE),
            postgresql_where=sa.text(OPEN_HELP_WHERE),
        )

    inspector = inspect(bind)
    if not _table_exists(inspector, 'help_request_events'):
        op.create_table(
            'help_request_events',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('help_request_id', sa.Integer(), sa.ForeignKey('help_requests.id'), nullable=False),
            sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id')),
            sa.Column('actor_role', sa.String(length=24), nullable=False),
            sa.Column('from_status', sa.String(length=24)),
            sa.Column('to_status', sa.String(length=24), nullable=False),
            sa.Column('event_type', sa.String(length=32), nullable=False),
            sa.Column('channel', sa.String(length=24), nullable=False),
            sa.Column('meta_json', sa.Text()),
            sa.Column('created_at', sa.DateTime()),
        )
        op.create_index('ix_help_request_events_request_id', 'help_request_events', ['help_request_id'])
        op.create_index('ix_help_request_events_created_at', 'help_request_events', ['created_at'])

    inspector = inspect(bind)
    if not _table_exists(inspector, 'notification_outbox'):
        op.create_table(
            'notification_outbox',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('help_request_id', sa.Integer(), sa.ForeignKey('help_requests.id')),
            sa.Column('help_event_id', sa.Integer(), sa.ForeignKey('help_request_events.id')),
            sa.Column('recipient_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('channel', sa.String(length=24), nullable=False),
            sa.Column('event_type', sa.String(length=32), nullable=False),
            sa.Column('dedupe_key', sa.String(length=160), nullable=False),
            sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
            sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_attempt_at', sa.DateTime()),
            sa.Column('last_error_type', sa.String(length=64)),
            sa.Column('provider_accepted_at', sa.DateTime()),
            sa.Column('opened_at', sa.DateTime()),
            sa.Column('created_at', sa.DateTime()),
            sa.Column('updated_at', sa.DateTime()),
            sa.UniqueConstraint('dedupe_key', name='uq_notification_outbox_dedupe_key'),
        )
        op.create_index(
            'ix_notification_outbox_status_next',
            'notification_outbox',
            ['status', 'next_attempt_at'],
        )
        op.create_index('ix_notification_outbox_recipient', 'notification_outbox', ['recipient_user_id'])

    inspector = inspect(bind)
    if not _table_exists(inspector, 'api_idempotency_keys'):
        op.create_table(
            'api_idempotency_keys',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('scope', sa.String(length=80), nullable=False),
            sa.Column('key', sa.String(length=80), nullable=False),
            sa.Column('request_hash', sa.String(length=64), nullable=False),
            sa.Column('resource_type', sa.String(length=32), nullable=False),
            sa.Column('resource_public_id', sa.String(length=32)),
            sa.Column('response_json', sa.Text()),
            sa.Column('created_at', sa.DateTime()),
            sa.UniqueConstraint('scope', 'key', name='uq_api_idempotency_scope_key'),
        )

    inspector = inspect(bind)
    if not _table_exists(inspector, 'miniprogram_identities'):
        op.create_table(
            'miniprogram_identities',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('openid_hash', sa.String(length=64), nullable=False),
            sa.Column('privacy_consent_version', sa.String(length=64), nullable=False),
            sa.Column('privacy_consented_at', sa.DateTime(), nullable=False),
            sa.Column('acquisition_source', sa.String(length=20), nullable=False, server_default='direct'),
            sa.Column('created_at', sa.DateTime()),
            sa.Column('last_login_at', sa.DateTime()),
            sa.UniqueConstraint('openid_hash', name='uq_miniprogram_identities_openid_hash'),
            sa.UniqueConstraint('id', 'user_id', name='uq_miniprogram_identities_id_user_id'),
        )
        op.create_index('ix_miniprogram_identities_user_id', 'miniprogram_identities', ['user_id'])

    inspector = inspect(bind)
    if not _table_exists(inspector, 'miniprogram_sessions'):
        op.create_table(
            'miniprogram_sessions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('identity_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('privacy_consent_version', sa.String(length=64), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime()),
            sa.Column('last_used_at', sa.DateTime()),
            sa.Column('revoked_at', sa.DateTime()),
            sa.UniqueConstraint('token_hash', name='uq_miniprogram_sessions_token_hash'),
            sa.ForeignKeyConstraint(
                ['identity_id', 'user_id'],
                ['miniprogram_identities.id', 'miniprogram_identities.user_id'],
                name='fk_miniprogram_sessions_identity_owner',
            ),
        )
        op.create_index('ix_miniprogram_sessions_user_id', 'miniprogram_sessions', ['user_id'])
        op.create_index('ix_miniprogram_sessions_expires_at', 'miniprogram_sessions', ['expires_at'])

    inspector = inspect(bind)
    if _table_exists(inspector, 'action_events'):
        _add_columns(
            'action_events',
            [('help_request_id', sa.Column('help_request_id', sa.Integer(), nullable=True))],
            _column_names(inspector, 'action_events'),
        )
        inspector = inspect(bind)
        if not _index_exists(inspector, 'action_events', 'ix_action_events_help_request_id'):
            op.create_index('ix_action_events_help_request_id', 'action_events', ['help_request_id'])


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if _table_exists(inspector, 'action_events') and 'help_request_id' in _column_names(inspector, 'action_events'):
        if bind.dialect.name == 'sqlite':
            with op.batch_alter_table('action_events') as batch_op:
                batch_op.drop_column('help_request_id')
        else:
            op.drop_column('action_events', 'help_request_id')
    for table in (
        'miniprogram_sessions',
        'miniprogram_identities',
        'api_idempotency_keys',
        'notification_outbox',
        'help_request_events',
        'help_requests',
        'family_invites',
        'family_memberships',
        'family_spaces',
    ):
        inspector = inspect(bind)
        if _table_exists(inspector, table):
            op.drop_table(table)
    inspector = inspect(bind)
    if _table_exists(inspector, 'pairs') and 'family_space_id' in _column_names(inspector, 'pairs'):
        if bind.dialect.name == 'sqlite':
            with op.batch_alter_table('pairs') as batch_op:
                batch_op.drop_column('family_space_id')
        else:
            op.drop_column('pairs', 'family_space_id')
