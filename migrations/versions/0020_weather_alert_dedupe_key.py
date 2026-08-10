"""为天气预警增加并发幂等键

Revision ID: 0020_weather_alert_dedupe_key
Revises: 0019_daily_status_elder_actions
Create Date: 2026-07-18 14:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0020_weather_alert_dedupe_key'
down_revision = '0019_daily_status_elder_actions'
branch_labels = None
depends_on = None


CONSTRAINT_NAME = 'uq_weather_alerts_dedupe_key'


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'weather_alerts' not in inspector.get_table_names():
        return

    columns = {column['name'] for column in inspector.get_columns('weather_alerts')}
    if 'dedupe_key' not in columns:
        op.add_column(
            'weather_alerts',
            sa.Column('dedupe_key', sa.String(length=64), nullable=True),
        )
    indexes = {item.get('name') for item in inspect(bind).get_indexes('weather_alerts')}
    if CONSTRAINT_NAME not in indexes:
        op.create_index(
            CONSTRAINT_NAME,
            'weather_alerts',
            ['dedupe_key'],
            unique=True,
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'weather_alerts' not in inspector.get_table_names():
        return
    columns = {column['name'] for column in inspector.get_columns('weather_alerts')}
    if 'dedupe_key' not in columns:
        return

    protected_count = int(
        bind.execute(
            sa.text(
                '''
                SELECT COUNT(*)
                FROM weather_alerts
                WHERE dedupe_key IS NOT NULL
                  AND TRIM(dedupe_key) != ''
                '''
            )
        ).scalar_one()
    )
    if protected_count:
        raise RuntimeError(
            'weather alert dedupe downgrade aborted: '
            f'protected_count={protected_count}; dedupe_key was preserved'
        )

    indexes = {item.get('name') for item in inspector.get_indexes('weather_alerts')}
    if CONSTRAINT_NAME in indexes:
        op.drop_index(CONSTRAINT_NAME, table_name='weather_alerts')
    op.drop_column('weather_alerts', 'dedupe_key')
