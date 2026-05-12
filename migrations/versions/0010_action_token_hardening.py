"""action token hardening

Revision ID: 0010_action_token_hardening
Revises: 0009_pilot_loop
Create Date: 2026-05-12 00:00:00.000000
"""

from datetime import datetime, timedelta, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '0010_action_token_hardening'
down_revision = '0009_pilot_loop'
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

    if _table_exists(inspector, 'pairs') and not _column_exists(inspector, 'pairs', 'short_code_expires_at'):
        op.add_column('pairs', sa.Column('short_code_expires_at', sa.DateTime(), nullable=True))
        expires_at = datetime.now(timezone.utc) + timedelta(days=90)
        bind.execute(
            sa.text(
                "UPDATE pairs "
                "SET short_code_expires_at = :expires_at "
                "WHERE status = 'active' AND short_code_expires_at IS NULL"
            ),
            {'expires_at': expires_at},
        )

    if not _table_exists(inspector, 'pair_action_tokens'):
        op.create_table(
            'pair_action_tokens',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id'), nullable=False),
            sa.Column('token_hash', sa.String(length=128), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('used_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('token_hash', name='uq_pair_action_tokens_token_hash'),
        )
        op.create_index('ix_pair_action_tokens_pair_id', 'pair_action_tokens', ['pair_id'])
        op.create_index('ix_pair_action_tokens_token_hash', 'pair_action_tokens', ['token_hash'])
        op.create_index('ix_pair_action_tokens_expires_at', 'pair_action_tokens', ['expires_at'])
    else:
        if not _index_exists(inspector, 'pair_action_tokens', 'ix_pair_action_tokens_pair_id'):
            op.create_index('ix_pair_action_tokens_pair_id', 'pair_action_tokens', ['pair_id'])
        if not _index_exists(inspector, 'pair_action_tokens', 'ix_pair_action_tokens_token_hash'):
            op.create_index('ix_pair_action_tokens_token_hash', 'pair_action_tokens', ['token_hash'])
        if not _index_exists(inspector, 'pair_action_tokens', 'ix_pair_action_tokens_expires_at'):
            op.create_index('ix_pair_action_tokens_expires_at', 'pair_action_tokens', ['expires_at'])


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'pair_action_tokens'):
        if _index_exists(inspector, 'pair_action_tokens', 'ix_pair_action_tokens_expires_at'):
            op.drop_index('ix_pair_action_tokens_expires_at', table_name='pair_action_tokens')
        if _index_exists(inspector, 'pair_action_tokens', 'ix_pair_action_tokens_token_hash'):
            op.drop_index('ix_pair_action_tokens_token_hash', table_name='pair_action_tokens')
        if _index_exists(inspector, 'pair_action_tokens', 'ix_pair_action_tokens_pair_id'):
            op.drop_index('ix_pair_action_tokens_pair_id', table_name='pair_action_tokens')
        op.drop_table('pair_action_tokens')

    if _table_exists(inspector, 'pairs') and _column_exists(inspector, 'pairs', 'short_code_expires_at'):
        op.drop_column('pairs', 'short_code_expires_at')
