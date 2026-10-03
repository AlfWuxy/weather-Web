"""新增跨进程资源预算及地理编码查询租约。"""
from alembic import op
import sqlalchemy as sa

revision = '0011_security_budgets'
down_revision = '0010_action_token_hardening'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    schemas = {
        'resource_budgets': [sa.Column('key', sa.String(180), primary_key=True),
                             sa.Column('used', sa.BigInteger(), nullable=False),
                             sa.Column('expires_at', sa.DateTime(), nullable=False)],
        'resource_leases': [sa.Column('key', sa.String(80), primary_key=True),
                            sa.Column('owner', sa.String(64), nullable=False),
                            sa.Column('expires_at', sa.DateTime(), nullable=False)],
    }
    for name, columns in schemas.items():
        inspector = sa.inspect(bind)
        if name not in inspector.get_table_names():
            op.create_table(name, *columns)
        else:
            # 部署脚本会先 create_all；仅接受与当前模型一致的预建表。
            actual = {item['name']: item for item in inspector.get_columns(name)}
            primary_key = inspector.get_pk_constraint(name).get('constrained_columns')
            if set(actual) != {column.name for column in columns} or primary_key != ['key']:
                raise RuntimeError(f'{name} 已存在但结构不兼容，停止迁移')
            for column in columns:
                found = actual[column.name]
                expected_type = column.type.compile(dialect=bind.dialect)
                actual_type = found['type'].compile(dialect=bind.dialect)
                if expected_type != actual_type or found['nullable'] != column.nullable:
                    raise RuntimeError(f'{name}.{column.name} 结构不兼容，停止迁移')
    index_name = 'ix_resource_budgets_expires_at'
    indexes = {index['name']: index for index in sa.inspect(bind).get_indexes('resource_budgets')}
    if index_name not in indexes:
        op.create_index(index_name, 'resource_budgets', ['expires_at'])
    elif indexes[index_name]['column_names'] != ['expires_at']:
        raise RuntimeError('资源预算清理索引结构不兼容，停止迁移')
    if 'location_cache' in sa.inspect(bind).get_table_names():
        # 删除历史冗余供应商响应，保留业务地点输入及解析坐标。
        bind.execute(sa.text('UPDATE location_cache SET raw_json = NULL WHERE raw_json IS NOT NULL'))


def downgrade():
    op.drop_table('resource_leases')
    op.drop_index('ix_resource_budgets_expires_at', table_name='resource_budgets')
    op.drop_table('resource_budgets')
