"""short code hash fields

Revision ID: 0007_short_code_hash
Revises: 0006_cooling_resource_fields
Create Date: 2025-02-05 00:00:00.000000
"""

import hashlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from core.app import create_app


# revision identifiers, used by Alembic.
revision = '0007_short_code_hash'
down_revision = '0006_cooling_resource_fields'
branch_labels = None
depends_on = None


_APP = create_app()
_PAIR_TOKEN_PEPPER = _APP.config.get('PAIR_TOKEN_PEPPER', '') or ''


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name, column_name):
    try:
        columns = inspector.get_columns(table_name)
    except Exception:
        return False
    return any(column.get('name') == column_name for column in columns)


def _index_exists(inspector, table_name, index_name):
    try:
        indexes = inspector.get_indexes(table_name)
    except Exception:
        return False
    return any(index.get('name') == index_name for index in indexes)


def _hash_short_code(value):
    if not value:
        return None
    payload = f"{value}{_PAIR_TOKEN_PEPPER}".encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _backfill_short_code_hash(bind, table_name):
    table = sa.table(
        table_name,
        sa.column('id', sa.Integer),
        sa.column('short_code', sa.String),
        sa.column('short_code_hash', sa.String)
    )
    rows = bind.execute(
        sa.select(table.c.id, table.c.short_code).where(table.c.short_code_hash.is_(None))
    ).fetchall()
    for row in rows:
        digest = _hash_short_code(row.short_code)
        if not digest:
            continue
        bind.execute(
            table.update()
            .where(table.c.id == row.id)
            .values(short_code_hash=digest)
        )


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'pairs'):
        has_pairs_hash = _column_exists(inspector, 'pairs', 'short_code_hash')
        if not has_pairs_hash:
            op.add_column('pairs', sa.Column('short_code_hash', sa.String(length=64)))
            has_pairs_hash = True
        if has_pairs_hash and not _index_exists(inspector, 'pairs', 'ix_pairs_short_code_hash'):
            op.create_index('ix_pairs_short_code_hash', 'pairs', ['short_code_hash'])
        if has_pairs_hash:
            _backfill_short_code_hash(bind, 'pairs')

    if _table_exists(inspector, 'pair_links'):
        has_links_hash = _column_exists(inspector, 'pair_links', 'short_code_hash')
        if not has_links_hash:
            op.add_column('pair_links', sa.Column('short_code_hash', sa.String(length=64)))
            has_links_hash = True
        if has_links_hash and not _index_exists(inspector, 'pair_links', 'ix_pair_links_short_code_hash'):
            op.create_index('ix_pair_links_short_code_hash', 'pair_links', ['short_code_hash'])
        if has_links_hash:
            _backfill_short_code_hash(bind, 'pair_links')


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'pair_links'):
        if _index_exists(inspector, 'pair_links', 'ix_pair_links_short_code_hash'):
            op.drop_index('ix_pair_links_short_code_hash', table_name='pair_links')
        if _column_exists(inspector, 'pair_links', 'short_code_hash'):
            op.drop_column('pair_links', 'short_code_hash')

    if _table_exists(inspector, 'pairs'):
        if _index_exists(inspector, 'pairs', 'ix_pairs_short_code_hash'):
            op.drop_index('ix_pairs_short_code_hash', table_name='pairs')
        if _column_exists(inspector, 'pairs', 'short_code_hash'):
            op.drop_column('pairs', 'short_code_hash')
