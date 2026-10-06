"""追加避暑资源服务核验，坐标核验不能代替营业状态核验。"""
import importlib

from alembic import op
import sqlalchemy as sa

revision = '0036_cooling_service_verify'
down_revision = '0035_integrity_accounts'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not {'cooling_resources', 'pairs'} <= set(sa.inspect(bind).get_table_names()):
        raise RuntimeError('避暑服务核验迁移缺少核心表')
    helpers = importlib.import_module('migrations.versions.0035_integrity_accounts')
    helpers._columns('cooling_resources', [
        sa.Column('last_verified_at', sa.DateTime(), nullable=True),
        sa.Column('verified_by_role', sa.String(16), nullable=True),
        sa.Column('verify_method', sa.String(16), nullable=True),
        sa.Column('open_during_alert', sa.String(16), nullable=True),
        sa.Column('alert_open_note_code', sa.String(32), nullable=True),
        sa.Column('amenities_json', sa.Text(), nullable=True),
        sa.Column('transport_need', sa.String(16), nullable=True),
        sa.Column('verify_status', sa.String(16), nullable=True),
    ])
    helpers._table('cooling_feedback', [
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('resource_id', sa.Integer(), sa.ForeignKey('cooling_resources.id'), nullable=False),
        sa.Column('pair_id', sa.Integer(), sa.ForeignKey('pairs.id'), nullable=True),
        sa.Column('code', sa.String(16), nullable=False),
        sa.Column('channel', sa.String(24), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    ], [])
    indexes = {row['name']: row for row in sa.inspect(bind).get_indexes('cooling_feedback')}
    for column in ('resource_id', 'pair_id', 'code', 'created_at'):
        name = f'ix_cooling_feedback_{column}'
        if name not in indexes:
            op.create_index(name, 'cooling_feedback', [column])
        elif indexes[name]['column_names'] != [column] or indexes[name].get('unique'):
            raise RuntimeError(f'避暑反馈索引不兼容: {name}')
    # 历史服务状态保持未知；不凭已有坐标或 is_active 自动签发核验回执。


def downgrade():
    importlib.import_module('migrations.versions.0035_integrity_accounts')._preflight_lower_downgrade()
    # 仅回退版本标记；旧应用可忽略追加字段，核验回执与反馈原样保留。
