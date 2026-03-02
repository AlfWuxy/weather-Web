"""weather cache/data unique constraints

Revision ID: 0008_weather_uniques
Revises: 0007_short_code_hash
Create Date: 2025-02-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '0008_weather_uniques'
down_revision = '0007_short_code_hash'
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def _unique_exists(inspector, table_name, constraint_name):
    try:
        constraints = inspector.get_unique_constraints(table_name)
    except Exception:
        return False
    return any(item.get('name') == constraint_name for item in constraints)


def _create_unique_constraint(bind, inspector, table_name, constraint_name, columns):
    if _unique_exists(inspector, table_name, constraint_name):
        return
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_unique_constraint(constraint_name, columns)
    else:
        op.create_unique_constraint(constraint_name, table_name, columns)


def _drop_unique_constraint(bind, inspector, table_name, constraint_name):
    if not _unique_exists(inspector, table_name, constraint_name):
        return
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(constraint_name, type_='unique')
    else:
        op.drop_constraint(constraint_name, table_name, type_='unique')


def _chunked(items, size=500):
    for idx in range(0, len(items), size):
        yield items[idx:idx + size]


def _dedupe_weather_cache(bind):
    table = sa.table(
        'weather_cache',
        sa.column('id', sa.Integer),
        sa.column('location', sa.String),
        sa.column('fetched_at', sa.DateTime)
    )
    rows = bind.execute(
        sa.select(table.c.id, table.c.location, table.c.fetched_at)
    ).fetchall()
    if not rows:
        return
    keep = {}
    for row in rows:
        location = row.location
        fetched_at = row.fetched_at
        if not location:
            continue
        existing = keep.get(location)
        if existing is None:
            keep[location] = (fetched_at, row.id)
            continue
        existing_fetched, existing_id = existing
        if existing_fetched is None and fetched_at is None:
            if row.id > existing_id:
                keep[location] = (existing_fetched, row.id)
            continue
        if existing_fetched is None and fetched_at is not None:
            keep[location] = (fetched_at, row.id)
            continue
        if fetched_at is None:
            continue
        if fetched_at > existing_fetched or (fetched_at == existing_fetched and row.id > existing_id):
            keep[location] = (fetched_at, row.id)
    keep_ids = {value[1] for value in keep.values()}
    delete_ids = [row.id for row in rows if row.id not in keep_ids]
    for batch in _chunked(delete_ids):
        bind.execute(table.delete().where(table.c.id.in_(batch)))


def _dedupe_weather_data(bind):
    table = sa.table(
        'weather_data',
        sa.column('id', sa.Integer),
        sa.column('date', sa.Date),
        sa.column('location', sa.String)
    )
    rows = bind.execute(
        sa.select(table.c.id, table.c.date, table.c.location)
    ).fetchall()
    if not rows:
        return
    keep = {}
    for row in rows:
        key = (row.date, row.location)
        if key in keep:
            if row.id > keep[key]:
                keep[key] = row.id
        else:
            keep[key] = row.id
    keep_ids = set(keep.values())
    delete_ids = [row.id for row in rows if row.id not in keep_ids]
    for batch in _chunked(delete_ids):
        bind.execute(table.delete().where(table.c.id.in_(batch)))


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'weather_cache'):
        _dedupe_weather_cache(bind)
        _create_unique_constraint(
            bind,
            inspector,
            'weather_cache',
            'uq_weather_cache_location',
            ['location']
        )

    if _table_exists(inspector, 'weather_data'):
        _dedupe_weather_data(bind)
        _create_unique_constraint(
            bind,
            inspector,
            'weather_data',
            'uq_weather_data_date_location',
            ['date', 'location']
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if _table_exists(inspector, 'weather_data'):
        _drop_unique_constraint(bind, inspector, 'weather_data', 'uq_weather_data_date_location')

    if _table_exists(inspector, 'weather_cache'):
        _drop_unique_constraint(bind, inspector, 'weather_cache', 'uq_weather_cache_location')
