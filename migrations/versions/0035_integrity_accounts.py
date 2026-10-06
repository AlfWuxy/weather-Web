"""生产链追加账号恢复与成员健康同意，保留历史用户和回执。"""
from alembic import op
import sqlalchemy as sa

revision = '0035_integrity_accounts'
down_revision = '0034_security_budgets'
branch_labels = None
depends_on = None


def _validate_column(actual, expected):
    if (not isinstance(actual['type'], type(expected.type))
            or (isinstance(expected.type, sa.String) and actual['type'].length != expected.type.length)
            or (not expected.primary_key and actual['nullable'] != expected.nullable)):
        raise RuntimeError(f'账号迁移列结构不兼容: {expected.name}')


def _columns(table, specs):
    bind = op.get_bind()
    existing = {item['name']: item for item in sa.inspect(bind).get_columns(table)}
    for spec in specs:
        if spec.name in existing:
            _validate_column(existing[spec.name], spec)
        else:
            op.add_column(table, spec)


def _table(name, columns, constraints):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if name not in inspector.get_table_names():
        op.create_table(name, *columns, *constraints)
        return
    actual = {item['name']: item for item in inspector.get_columns(name)}
    if set(actual) != {column.name for column in columns}:
        raise RuntimeError(f'账号迁移表结构不兼容: {name}')
    for column in columns:
        _validate_column(actual[column.name], column)
    if inspector.get_pk_constraint(name)['constrained_columns'] != ['id']:
        raise RuntimeError(f'账号迁移主键不兼容: {name}')
    uniques = {tuple(row['column_names']) for row in inspector.get_unique_constraints(name)}
    uniques |= {tuple(row['column_names']) for row in inspector.get_indexes(name) if row.get('unique')}
    for constraint in constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            expected = tuple(constraint.columns.keys()) or tuple(constraint._pending_colargs)
            if expected not in uniques:
                raise RuntimeError(f'账号迁移唯一约束缺失: {name}')
    foreign_keys = {(tuple(row['constrained_columns']), row['referred_table'], tuple(row['referred_columns'])) for row in inspector.get_foreign_keys(name)}
    for column in columns:
        for fk in column.foreign_keys:
            table, key = fk.target_fullname.split('.')
            if ((column.name,), table, (key,)) not in foreign_keys:
                raise RuntimeError(f'账号迁移外键缺失: {name}.{column.name}')


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if not {'users', 'family_members'} <= tables:
        raise RuntimeError('账号迁移缺少核心表')
    _columns('users', [sa.Column('email_verified_at', sa.DateTime(), nullable=True)])
    _columns('family_members', [
        sa.Column('health_sensitive_consent_version', sa.String(64), nullable=True),
        sa.Column('health_sensitive_consented_at', sa.DateTime(), nullable=True)])
    _table('recovery_delegates', [
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('delegate_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('verified_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True)],
        [sa.UniqueConstraint('user_id', 'delegate_id', name='uq_recovery_delegate')])
    _table('account_email_tokens', [
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('purpose', sa.String(16), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False),
        sa.Column('email_hash', sa.String(64), nullable=False),
        sa.Column('password_stamp', sa.String(64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True)],
        [sa.UniqueConstraint('token_hash')])
    indexes = {row['name']: row for row in sa.inspect(op.get_bind()).get_indexes('account_email_tokens')}
    name = 'ix_account_email_tokens_user_id'
    if name not in indexes:
        op.create_index(name, 'account_email_tokens', ['user_id'])
    elif indexes[name]['column_names'] != ['user_id']:
        raise RuntimeError('账号邮件索引不兼容')
    # 不修改旧回执，不从一般隐私或旧健康资料推断新授权。


def _preflight_lower_downgrade():
    # 新 head 的首个版本标记变更前，执行生产旧链的全部目标范围保护。
    import importlib
    bind = op.get_bind()
    context = op.get_context()
    environment = getattr(context, 'environment_context', None)
    if environment is None:
        return
    planned = {item.revision for item in environment.script.iterate_revisions(
        context.get_current_heads(), environment.get_revision_argument(), select_for_downgrade=True)}
    if '0033_institution_data_pilot' in planned:
        previous = importlib.import_module('migrations.versions.0033_institution_data_pilot')
        if not previous._validate_existing(bind):
            raise RuntimeError('pilot migration aborted: missing_schema')
        previous._preflight_lower_downgrade(bind)
        for name in previous.TABLE_NAMES:
            if bind.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar_one():
                raise RuntimeError('试点表包含记录，请保留数据并仅关闭功能开关')
    if '0034_security_budgets' in planned:
        if bind.execute(sa.text('SELECT COUNT(*) FROM resource_budgets')).scalar_one():
            raise RuntimeError('资源预算存在记录，降级会重置额度；请保留表并仅回滚应用代码')


def downgrade():
    _preflight_lower_downgrade()
    # 仅回退应用版本标记，追加结构保留，避免丢失恢复关系和回执。
