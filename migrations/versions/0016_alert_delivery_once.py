"""预警投递使用数据库占位保证单次外呼

Revision ID: 0016_alert_delivery_once
Revises: 0015_miniprogram_acquisition
Create Date: 2026-07-18 03:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0016_alert_delivery_once'
down_revision = '0015_miniprogram_acquisition'
branch_labels = None
depends_on = None


CONSTRAINT_NAME = 'uq_alert_deliveries_alert_user_channel'


def _has_delivery_constraint(inspector):
    constraints = {
        item.get('name')
        for item in inspector.get_unique_constraints('alert_deliveries')
    }
    indexes = {
        item.get('name')
        for item in inspector.get_indexes('alert_deliveries')
        if item.get('unique')
    }
    return CONSTRAINT_NAME in constraints or CONSTRAINT_NAME in indexes


def _normalize_legacy_rows(bind):
    """先收敛旧状态，再按确定送达优先、同级最新优先去重。"""
    bind.execute(
        sa.text(
            """
            UPDATE alert_deliveries
            SET channel = LOWER(TRIM(COALESCE(channel, 'wxpusher')))
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE alert_deliveries
            SET channel = 'wxpusher'
            WHERE channel = ''
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE alert_deliveries
            SET status = 'uncertain',
                error = COALESCE(error, '历史投递状态不明确，禁止自动重试')
            WHERE status IS NULL
               OR status NOT IN ('sending', 'sent', 'failed', 'uncertain')
            """
        )
    )

    rows = bind.execute(
        sa.text(
            """
            SELECT id, alert_id, user_id, channel
            FROM alert_deliveries
            ORDER BY alert_id,
                     user_id,
                     channel,
                     CASE WHEN status = 'sent' THEN 0 ELSE 1 END,
                     CASE WHEN sent_at IS NULL THEN 1 ELSE 0 END,
                     sent_at DESC,
                     id DESC
            """
        )
    )
    seen = set()
    duplicate_ids = []
    for row in rows:
        key = (row.alert_id, row.user_id, row.channel)
        if key in seen:
            duplicate_ids.append(int(row.id))
        else:
            seen.add(key)
    if duplicate_ids:
        bind.execute(
            sa.text('DELETE FROM alert_deliveries WHERE id = :delivery_id'),
            [{'delivery_id': delivery_id} for delivery_id in duplicate_ids],
        )


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'alert_deliveries' not in inspector.get_table_names():
        return

    _normalize_legacy_rows(bind)
    inspector = inspect(bind)
    constraint_exists = _has_delivery_constraint(inspector)

    if bind.dialect.name == 'sqlite':
        # SQLite 通过 batch copy-and-move 同时固化非空字段和命名唯一约束。
        with op.batch_alter_table('alert_deliveries', recreate='always') as batch_op:
            batch_op.alter_column(
                'channel',
                existing_type=sa.String(length=20),
                nullable=False,
                server_default='wxpusher',
            )
            batch_op.alter_column(
                'status',
                existing_type=sa.String(length=20),
                nullable=False,
                server_default='uncertain',
            )
            if not constraint_exists:
                batch_op.create_unique_constraint(
                    CONSTRAINT_NAME,
                    ['alert_id', 'user_id', 'channel'],
                )
        return

    op.alter_column(
        'alert_deliveries',
        'channel',
        existing_type=sa.String(length=20),
        nullable=False,
        server_default='wxpusher',
    )
    op.alter_column(
        'alert_deliveries',
        'status',
        existing_type=sa.String(length=20),
        nullable=False,
        server_default='uncertain',
    )
    if not constraint_exists:
        op.create_unique_constraint(
            CONSTRAINT_NAME,
            'alert_deliveries',
            ['alert_id', 'user_id', 'channel'],
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'alert_deliveries' not in inspector.get_table_names():
        return
    constraint_exists = _has_delivery_constraint(inspector)

    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table('alert_deliveries', recreate='always') as batch_op:
            if constraint_exists:
                batch_op.drop_constraint(CONSTRAINT_NAME, type_='unique')
            batch_op.alter_column(
                'channel',
                existing_type=sa.String(length=20),
                nullable=True,
                server_default=None,
            )
            batch_op.alter_column(
                'status',
                existing_type=sa.String(length=20),
                nullable=True,
                server_default=None,
            )
        return

    if constraint_exists:
        op.drop_constraint(
            CONSTRAINT_NAME,
            'alert_deliveries',
            type_='unique',
        )
    op.alter_column(
        'alert_deliveries',
        'channel',
        existing_type=sa.String(length=20),
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        'alert_deliveries',
        'status',
        existing_type=sa.String(length=20),
        nullable=True,
        server_default=None,
    )
