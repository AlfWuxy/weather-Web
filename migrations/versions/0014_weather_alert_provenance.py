"""add weather alert provenance and validity window

Revision ID: 0014_weather_alert_provenance
Revises: 0013_air_quality_observed_at
Create Date: 2026-08-28 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0014_weather_alert_provenance'
down_revision = '0013_air_quality_observed_at'
branch_labels = None
depends_on = None


def _column_names(inspector):
    if 'weather_alerts' not in inspector.get_table_names():
        return set()
    return {column.get('name') for column in inspector.get_columns('weather_alerts')}


def upgrade():
    bind = op.get_bind()
    columns = _column_names(inspect(bind))
    if not columns:
        return

    # 旧记录保留原样并默认非官方，禁止凭文案推断来源或有效期。
    if 'source' not in columns:
        op.add_column(
            'weather_alerts',
            sa.Column('source', sa.String(length=50), nullable=True),
        )
    if 'is_official' not in columns:
        op.add_column(
            'weather_alerts',
            sa.Column(
                'is_official',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if 'starts_at' not in columns:
        op.add_column(
            'weather_alerts',
            sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
        )
    if 'ends_at' not in columns:
        op.add_column(
            'weather_alerts',
            sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    columns = _column_names(inspect(bind))
    removable = [
        name
        for name in ('ends_at', 'starts_at', 'is_official', 'source')
        if name in columns
    ]
    if not removable:
        return
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table('weather_alerts') as batch_op:
            for name in removable:
                batch_op.drop_column(name)
        return
    for name in removable:
        op.drop_column('weather_alerts', name)
