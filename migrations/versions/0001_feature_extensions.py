"""feature extensions

Revision ID: 0001_feature_extensions
Revises: 
Create Date: 2025-01-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0001_feature_extensions'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('health_risk_assessments', sa.Column('explain', sa.Text(), nullable=True))

    op.create_table(
        'forecast_cache',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('location', sa.String(length=100), nullable=False),
        sa.Column('days', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.Column('payload', sa.Text(), nullable=True),
        sa.Column('is_mock', sa.Boolean(), nullable=True)
    )

    op.create_table(
        'notifications',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('member_id', sa.Integer(), sa.ForeignKey('family_members.id'), nullable=True),
        sa.Column('category', sa.String(length=50), nullable=True),
        sa.Column('title', sa.String(length=120), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('level', sa.String(length=20), nullable=True),
        sa.Column('action_url', sa.String(length=200), nullable=True),
        sa.Column('meta', sa.Text(), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=True, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=True)
    )

    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('actor_id', sa.Integer(), nullable=True),
        sa.Column('actor_role', sa.String(length=20), nullable=True),
        sa.Column('action', sa.String(length=80), nullable=False),
        sa.Column('resource_type', sa.String(length=80), nullable=True),
        sa.Column('resource_id', sa.String(length=80), nullable=True),
        sa.Column('metadata', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=200), nullable=True),
        sa.Column('request_id', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True)
    )


def downgrade():
    op.drop_table('audit_logs')
    op.drop_table('notifications')
    op.drop_table('forecast_cache')
    op.drop_column('health_risk_assessments', 'explain')
