"""action heat system tables

Revision ID: 0003_action_heat_system
Revises: 0002_schema_fixes
Create Date: 2025-01-08 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '0003_action_heat_system'
down_revision = '0002_schema_fixes'
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if not _table_exists(inspector, 'pairs'):
        op.create_table(
            'pairs',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('caregiver_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('community_code', sa.String(length=100), nullable=False),
            sa.Column('elder_code', sa.String(length=40), nullable=False),
            sa.Column('short_code', sa.String(length=12), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('last_active_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('elder_code', name='uq_pairs_elder_code'),
            sa.UniqueConstraint('short_code', name='uq_pairs_short_code')
        )
        op.create_index('ix_pairs_caregiver_id', 'pairs', ['caregiver_id'])
        op.create_index('ix_pairs_community_code', 'pairs', ['community_code'])

    if not _table_exists(inspector, 'pair_links'):
        op.create_table(
            'pair_links',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('caregiver_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('short_code', sa.String(length=12), nullable=False),
            sa.Column('token_hash', sa.String(length=128), nullable=False),
            sa.Column('community_code', sa.String(length=100), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=True),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('redeemed_at', sa.DateTime(), nullable=True),
            sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id')),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('short_code', name='uq_pair_links_short_code')
        )
        op.create_index('ix_pair_links_caregiver_id', 'pair_links', ['caregiver_id'])
        op.create_index('ix_pair_links_expires_at', 'pair_links', ['expires_at'])

    if not _table_exists(inspector, 'daily_status'):
        op.create_table(
            'daily_status',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id'), nullable=False),
            sa.Column('status_date', sa.Date(), nullable=False),
            sa.Column('community_code', sa.String(length=100), nullable=False),
            sa.Column('risk_level', sa.String(length=20)),
            sa.Column('confirmed_at', sa.DateTime()),
            sa.Column('help_flag', sa.Boolean(), nullable=True),
            sa.Column('actions_done_count', sa.Integer(), nullable=True),
            sa.Column('relay_stage', sa.String(length=20)),
            sa.Column('debrief_optin', sa.Boolean(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('pair_id', 'status_date', name='uq_daily_status_pair_date')
        )
        op.create_index('ix_daily_status_pair_date', 'daily_status', ['pair_id', 'status_date'])
        op.create_index('ix_daily_status_community_date', 'daily_status', ['community_code', 'status_date'])

    if not _table_exists(inspector, 'community_daily'):
        op.create_table(
            'community_daily',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('community_code', sa.String(length=100), nullable=False),
            sa.Column('date', sa.Date(), nullable=False),
            sa.Column('total_people', sa.Integer(), nullable=True),
            sa.Column('confirm_rate', sa.Float(), nullable=True),
            sa.Column('escalation_rate', sa.Float(), nullable=True),
            sa.Column('risk_distribution', sa.Text()),
            sa.Column('outreach_summary', sa.Text()),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('community_code', 'date', name='uq_community_daily_code_date')
        )
        op.create_index('ix_community_daily_code_date', 'community_daily', ['community_code', 'date'])

    if not _table_exists(inspector, 'cooling_resources'):
        op.create_table(
            'cooling_resources',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('community_code', sa.String(length=100), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('resource_type', sa.String(length=50)),
            sa.Column('address_hint', sa.String(length=200)),
            sa.Column('open_hours', sa.String(length=100)),
            sa.Column('contact_hint', sa.String(length=100)),
            sa.Column('notes', sa.Text()),
            sa.Column('is_active', sa.Boolean(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True)
        )
        op.create_index('ix_cooling_resources_community', 'cooling_resources', ['community_code'])

    if not _table_exists(inspector, 'debriefs'):
        op.create_table(
            'debriefs',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('date', sa.Date(), nullable=False),
            sa.Column('community_code', sa.String(length=100), nullable=False),
            sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id')),
            sa.Column('question_1', sa.String(length=200)),
            sa.Column('question_2', sa.String(length=200)),
            sa.Column('question_3', sa.String(length=200)),
            sa.Column('difficulty', sa.Text()),
            sa.Column('created_at', sa.DateTime(), nullable=True)
        )
        op.create_index('ix_debriefs_community_date', 'debriefs', ['community_code', 'date'])
        op.create_index('ix_debriefs_pair_date', 'debriefs', ['pair_id', 'date'])


def _drop_table_if_exists(table_name):
    bind = op.get_bind()
    inspector = inspect(bind)
    if not _table_exists(inspector, table_name):
        return
    op.drop_table(table_name)


def downgrade():
    _drop_table_if_exists('debriefs')
    _drop_table_if_exists('cooling_resources')
    _drop_table_if_exists('community_daily')
    _drop_table_if_exists('daily_status')
    _drop_table_if_exists('pair_links')
    _drop_table_if_exists('pairs')
