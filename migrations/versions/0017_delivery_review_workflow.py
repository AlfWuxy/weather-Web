"""增加投递人工复核与安全重试状态

Revision ID: 0017_delivery_review_workflow
Revises: 0016_alert_delivery_once
Create Date: 2026-07-18 06:20:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0017_delivery_review_workflow'
down_revision = '0016_alert_delivery_once'
branch_labels = None
depends_on = None


INDEX_NAME = 'ix_alert_deliveries_status_sent_at'
REVIEWER_FK_NAME = 'fk_alert_deliveries_reviewed_by_user_id_users'


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'alert_deliveries' not in inspector.get_table_names():
        return

    columns = {column['name'] for column in inspector.get_columns('alert_deliveries')}
    indexes = {index.get('name') for index in inspector.get_indexes('alert_deliveries')}
    with op.batch_alter_table('alert_deliveries') as batch_op:
        if 'attempt_count' not in columns:
            batch_op.add_column(sa.Column(
                'attempt_count',
                sa.Integer(),
                nullable=False,
                server_default='1',
            ))
        if 'reviewed_at' not in columns:
            batch_op.add_column(sa.Column('reviewed_at', sa.DateTime(), nullable=True))
        if 'reviewed_by_user_id' not in columns:
            batch_op.add_column(sa.Column(
                'reviewed_by_user_id',
                sa.Integer(),
                sa.ForeignKey(
                    'users.id',
                    name=REVIEWER_FK_NAME,
                    ondelete='SET NULL',
                ),
                nullable=True,
            ))
        if 'review_action' not in columns:
            batch_op.add_column(sa.Column('review_action', sa.String(length=20), nullable=True))
        if INDEX_NAME not in indexes:
            batch_op.create_index(INDEX_NAME, ['status', 'sent_at'], unique=False)


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'alert_deliveries' not in inspector.get_table_names():
        return

    columns = {column['name'] for column in inspector.get_columns('alert_deliveries')}
    indexes = {index.get('name') for index in inspector.get_indexes('alert_deliveries')}
    with op.batch_alter_table('alert_deliveries') as batch_op:
        if INDEX_NAME in indexes:
            batch_op.drop_index(INDEX_NAME)
        for name in ('review_action', 'reviewed_by_user_id', 'reviewed_at', 'attempt_count'):
            if name in columns:
                batch_op.drop_column(name)
