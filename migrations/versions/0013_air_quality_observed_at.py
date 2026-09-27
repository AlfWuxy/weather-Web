"""add independent air quality observation time

Revision ID: 0013_air_quality_observed_at
Revises: 0012_weather_data_provenance
Create Date: 2026-08-28 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0013_air_quality_observed_at'
down_revision = '0012_weather_data_provenance'
branch_labels = None
depends_on = None


def _column_names(inspector):
    if 'weather_data' not in inspector.get_table_names():
        return set()
    return {column.get('name') for column in inspector.get_columns('weather_data')}


def upgrade():
    bind = op.get_bind()
    columns = _column_names(inspect(bind))
    if not columns or 'air_observed_at' in columns:
        return

    # 旧行缺少独立空气观测来源，保持 NULL，禁止用天气时间伪造回填。
    op.add_column(
        'weather_data',
        sa.Column('air_observed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    bind = op.get_bind()
    columns = _column_names(inspect(bind))
    if 'air_observed_at' not in columns:
        return
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table('weather_data') as batch_op:
            batch_op.drop_column('air_observed_at')
        return
    op.drop_column('weather_data', 'air_observed_at')
