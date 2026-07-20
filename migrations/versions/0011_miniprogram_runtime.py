"""微信小程序运行时快照、身份和会话

Revision ID: 0011_miniprogram_runtime
Revises: 0010_action_token_hardening
Create Date: 2026-07-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0011_miniprogram_runtime'
down_revision = '0010_action_token_hardening'
branch_labels = None
depends_on = None


def _table_exists(inspector, name):
    return name in inspector.get_table_names()


def _column_exists(inspector, table_name, column_name):
    if not _table_exists(inspector, table_name):
        return False
    return any(column.get('name') == column_name for column in inspector.get_columns(table_name))


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'users') and not _column_exists(
        inspector, 'users', 'deleted_at'
    ):
        op.add_column('users', sa.Column('deleted_at', sa.DateTime(), nullable=True))

    if _table_exists(inspector, 'api_tokens'):
        if not _column_exists(inspector, 'api_tokens', 'expires_at'):
            op.add_column('api_tokens', sa.Column('expires_at', sa.DateTime(), nullable=True))
        if not _column_exists(inspector, 'api_tokens', 'scopes'):
            op.add_column('api_tokens', sa.Column('scopes', sa.String(length=200), nullable=True))
        if not _column_exists(inspector, 'api_tokens', 'privacy_consent_version'):
            op.add_column(
                'api_tokens',
                sa.Column('privacy_consent_version', sa.String(length=64), nullable=True),
            )
        existing_indexes = {index.get('name') for index in inspector.get_indexes('api_tokens')}
        if 'ix_api_tokens_expires_at' not in existing_indexes:
            op.create_index('ix_api_tokens_expires_at', 'api_tokens', ['expires_at'])

    if _table_exists(inspector, 'health_risk_assessments') and not _column_exists(
        inspector, 'health_risk_assessments', 'member_id'
    ):
        if bind.dialect.name == 'sqlite':
            # SQLite 通过 batch copy-and-move 同时建立列与命名外键。
            with op.batch_alter_table('health_risk_assessments') as batch_op:
                batch_op.add_column(sa.Column('member_id', sa.Integer(), nullable=True))
                batch_op.create_foreign_key(
                    'fk_health_risk_assessments_member_id',
                    'family_members',
                    ['member_id'],
                    ['id'],
                )
        else:
            op.add_column(
                'health_risk_assessments',
                sa.Column('member_id', sa.Integer(), nullable=True),
            )
            op.create_foreign_key(
                'fk_health_risk_assessments_member_id',
                'health_risk_assessments',
                'family_members',
                ['member_id'],
                ['id'],
            )

    if not _table_exists(inspector, 'miniprogram_snapshots'):
        op.create_table(
            'miniprogram_snapshots',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('snapshot_id', sa.String(length=36), nullable=False),
            sa.Column('location_name', sa.String(length=100), nullable=False),
            sa.Column('location_code', sa.String(length=100), nullable=False),
            sa.Column('fetched_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('available', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('current_json', sa.Text(), nullable=True),
            sa.Column('forecast_json', sa.Text(), nullable=True),
            sa.Column('warnings_json', sa.Text(), nullable=True),
            sa.Column('risk_json', sa.Text(), nullable=True),
            sa.Column('actions_json', sa.Text(), nullable=True),
            sa.Column('source_status_json', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('snapshot_id', name='uq_miniprogram_snapshots_snapshot_id'),
        )
        op.create_index(
            'ix_miniprogram_snapshots_fetched_at',
            'miniprogram_snapshots',
            ['fetched_at'],
        )
        op.create_index(
            'ix_miniprogram_snapshots_expires_at',
            'miniprogram_snapshots',
            ['expires_at'],
        )

    if not _table_exists(inspector, 'miniprogram_identities'):
        op.create_table(
            'miniprogram_identities',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'user_id',
                sa.Integer(),
                sa.ForeignKey('users.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('openid_hash', sa.String(length=64), nullable=False),
            sa.Column('privacy_consent_version', sa.String(length=64), nullable=False),
            sa.Column('privacy_consented_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('last_login_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('openid_hash', name='uq_miniprogram_identities_openid_hash'),
            sa.UniqueConstraint(
                'id',
                'user_id',
                name='uq_miniprogram_identities_id_user_id',
            ),
        )
        op.create_index(
            'ix_miniprogram_identities_user_id',
            'miniprogram_identities',
            ['user_id'],
        )
        op.create_index(
            'ix_miniprogram_identities_openid_hash',
            'miniprogram_identities',
            ['openid_hash'],
        )

    if not _table_exists(inspector, 'miniprogram_sessions'):
        op.create_table(
            'miniprogram_sessions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('identity_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('privacy_consent_version', sa.String(length=64), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ['identity_id', 'user_id'],
                ['miniprogram_identities.id', 'miniprogram_identities.user_id'],
                name='fk_miniprogram_sessions_identity_owner',
                ondelete='CASCADE',
            ),
            sa.UniqueConstraint('token_hash', name='uq_miniprogram_sessions_token_hash'),
        )
        op.create_index(
            'ix_miniprogram_sessions_identity_id',
            'miniprogram_sessions',
            ['identity_id'],
        )
        op.create_index(
            'ix_miniprogram_sessions_user_id',
            'miniprogram_sessions',
            ['user_id'],
        )
        op.create_index(
            'ix_miniprogram_sessions_token_hash',
            'miniprogram_sessions',
            ['token_hash'],
        )
        op.create_index(
            'ix_miniprogram_sessions_expires_at',
            'miniprogram_sessions',
            ['expires_at'],
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if _table_exists(inspector, 'miniprogram_sessions'):
        op.drop_table('miniprogram_sessions')
    if _table_exists(inspector, 'miniprogram_identities'):
        op.drop_table('miniprogram_identities')
    if _table_exists(inspector, 'miniprogram_snapshots'):
        op.drop_table('miniprogram_snapshots')
    inspector = inspect(bind)
    if _column_exists(inspector, 'health_risk_assessments', 'member_id'):
        if bind.dialect.name == 'sqlite':
            foreign_keys = inspector.get_foreign_keys('health_risk_assessments')
            member_fk = next(
                (
                    foreign_key
                    for foreign_key in foreign_keys
                    if foreign_key.get('constrained_columns') == ['member_id']
                ),
                None,
            )
            naming_convention = {
                'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s'
            }
            constraint_name = (
                member_fk.get('name')
                if member_fk and member_fk.get('name')
                else 'fk_health_risk_assessments_member_id_family_members'
            )
            with op.batch_alter_table(
                'health_risk_assessments',
                naming_convention=naming_convention,
            ) as batch_op:
                batch_op.drop_constraint(
                    constraint_name,
                    type_='foreignkey',
                )
                batch_op.drop_column('member_id')
        else:
            op.drop_constraint(
                'fk_health_risk_assessments_member_id',
                'health_risk_assessments',
                type_='foreignkey',
            )
            op.drop_column('health_risk_assessments', 'member_id')
    if _table_exists(inspector, 'api_tokens'):
        index_names = {index.get('name') for index in inspector.get_indexes('api_tokens')}
        if 'ix_api_tokens_expires_at' in index_names:
            op.drop_index('ix_api_tokens_expires_at', table_name='api_tokens')
        if _column_exists(inspector, 'api_tokens', 'privacy_consent_version'):
            op.drop_column('api_tokens', 'privacy_consent_version')
        if _column_exists(inspector, 'api_tokens', 'scopes'):
            op.drop_column('api_tokens', 'scopes')
        if _column_exists(inspector, 'api_tokens', 'expires_at'):
            op.drop_column('api_tokens', 'expires_at')
    if _column_exists(inspector, 'users', 'deleted_at'):
        op.drop_column('users', 'deleted_at')
