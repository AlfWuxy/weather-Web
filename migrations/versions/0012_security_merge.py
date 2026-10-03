"""合并身份与资源保护的独立迁移，保留两条升级路径。"""
revision = '0012_security_merge'
down_revision = ('0011_security_identity', '0011_security_budgets')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
