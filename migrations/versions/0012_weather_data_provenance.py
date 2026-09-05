"""add weather observation provenance

Revision ID: 0012_weather_data_provenance
Revises: 0011_authorized_community
Create Date: 2026-08-27 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0012_weather_data_provenance'
down_revision = '0011_authorized_community'
branch_labels = None
depends_on = None


def _column_names(inspector):
    if 'weather_data' not in inspector.get_table_names():
        return set()
    return {column.get('name') for column in inspector.get_columns('weather_data')}


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = _column_names(inspector)
    if not columns:
        return

    # 旧行不回填来源和观测时刻；默认 quality_version=0 明确标记为不可信。
    if 'data_source' not in columns:
        op.add_column(
            'weather_data',
            sa.Column('data_source', sa.String(length=32), nullable=True),
        )
    if 'observed_at' not in columns:
        op.add_column(
            'weather_data',
            sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        )
    if 'quality_version' not in columns:
        op.add_column(
            'weather_data',
            sa.Column(
                'quality_version',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            ),
        )
    if 'air_quality_available' not in columns:
        op.add_column(
            'weather_data',
            sa.Column(
                'air_quality_available',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = _column_names(inspector)
    removable = [
        name
        for name in (
            'air_quality_available',
            'quality_version',
            'observed_at',
            'data_source',
        )
        if name in columns
    ]
    if not removable:
        return
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table('weather_data') as batch_op:
            for name in removable:
                batch_op.drop_column(name)
        return
    for name in removable:
        op.drop_column('weather_data', name)
