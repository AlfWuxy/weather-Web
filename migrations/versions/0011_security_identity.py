"""凭证有限期限和可信社区授权。

Revision ID: 0011_security_identity
Revises: 0010_action_token_hardening
"""
from datetime import datetime, timedelta, timezone

from alembic import op
import sqlalchemy as sa

revision = '0011_security_identity'
down_revision = '0010_action_token_hardening'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'authorized_community' not in {c['name'] for c in inspector.get_columns('users')}:
        op.add_column('users', sa.Column('authorized_community', sa.String(100), nullable=True))
    # 不将可自行修改的 community 回填成授权；由管理员单独核准。
    if 'expires_at' not in {c['name'] for c in inspector.get_columns('api_tokens')}:
        op.add_column('api_tokens', sa.Column('expires_at', sa.DateTime(), nullable=True))
    now = datetime.now(timezone.utc)
    # 永久清理孤儿凭证，防止 SQLite 复用已删除用户 ID 后恢复授权。
    bind.execute(sa.text('DELETE FROM api_tokens WHERE NOT EXISTS (SELECT 1 FROM users WHERE users.id = api_tokens.user_id)'))
    bind.execute(sa.text('UPDATE api_tokens SET expires_at = :deadline WHERE expires_at IS NULL').bindparams(sa.bindparam('deadline', type_=sa.DateTime())),
                 {'deadline': now + timedelta(days=7)})


def downgrade():
    # 删除字段不会恢复已清理的孤儿凭证或此前撤销的凭证。
    op.drop_column('api_tokens', 'expires_at')
    op.drop_column('users', 'authorized_community')
