"""为避暑资源坐标增加来源与人工核验回执

Revision ID: 0026_cooling_coordinate_verify
Revises: 0025_health_sensitive_consent
Create Date: 2026-07-21 10:00:00.000000
"""

import importlib

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0026_cooling_coordinate_verify'
down_revision = '0025_health_sensitive_consent'
branch_labels = None
depends_on = None


COLUMN_SPECS = {
    'coordinate_system': (sa.String, 16),
    'coordinate_source': (sa.String, 500),
    'coordinate_verified_at': (sa.DateTime, None),
}


def _cooling_columns(inspector):
    return {
        column['name']: column
        for column in inspector.get_columns('cooling_resources')
    }


def _validate_existing_columns(columns):
    """拒绝把类型错误的同名列当作已完成迁移。"""
    invalid_columns = []
    for column_name, (expected_type, expected_length) in COLUMN_SPECS.items():
        column = columns.get(column_name)
        if column is None:
            continue
        column_type = column.get('type')
        invalid = not isinstance(column_type, expected_type)
        if expected_length is not None:
            invalid = invalid or getattr(column_type, 'length', None) != expected_length
        if invalid or column.get('nullable') is not True:
            invalid_columns.append(column_name)
    if invalid_columns:
        raise RuntimeError(
            'cooling coordinate verification migration aborted: invalid_columns='
            f'{sorted(invalid_columns)}; revision was not advanced'
        )


def _targets_only_previous_revision():
    """识别只回退本迁移的显式或相对目标。"""
    try:
        target = context.get_revision_argument()
    except Exception:
        # 脱离 Alembic 环境直接调用时保持最保守的整链预检。
        return False
    if isinstance(target, tuple):
        return len(target) == 1 and target[0] == down_revision
    return target in {down_revision, '-1'}


def _preflight_downgrade(bind=None, inspector=None, *, include_lower_chain=True):
    """在任何 DDL 前检查坐标核验及更早迁移的数据丢失风险。"""
    bind = bind or op.get_bind()
    inspector = inspector or inspect(bind)
    if include_lower_chain:
        previous_migration = importlib.import_module(
            'migrations.versions.0025_health_sensitive_consent'
        )
        previous_migration._preflight_downgrade(bind, inspector)

    if 'cooling_resources' not in inspector.get_table_names():
        return
    columns = _cooling_columns(inspector)
    _validate_existing_columns(columns)
    present_columns = set(COLUMN_SPECS) & set(columns)
    if not present_columns:
        return

    predicates = [f'{column_name} IS NOT NULL' for column_name in sorted(present_columns)]
    verification_count = bind.execute(sa.text(
        f'''SELECT COUNT(*) FROM cooling_resources
            WHERE {' OR '.join(predicates)}'''
    )).scalar_one()
    if verification_count:
        raise RuntimeError(
            'cooling coordinate verification downgrade aborted: '
            f'nonempty_verification_count={verification_count}'
        )


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'cooling_resources' not in inspector.get_table_names():
        raise RuntimeError(
            "cooling coordinate verification migration aborted: "
            "missing_tables=['cooling_resources']"
        )

    columns = _cooling_columns(inspector)
    _validate_existing_columns(columns)
    if 'coordinate_system' not in columns:
        op.add_column(
            'cooling_resources',
            sa.Column('coordinate_system', sa.String(length=16), nullable=True),
        )
    if 'coordinate_source' not in columns:
        op.add_column(
            'cooling_resources',
            sa.Column('coordinate_source', sa.String(length=500), nullable=True),
        )
    if 'coordinate_verified_at' not in columns:
        op.add_column(
            'cooling_resources',
            sa.Column('coordinate_verified_at', sa.DateTime(), nullable=True),
        )

    # 历史坐标没有可核验来源与人工确认时间，统一保持未核验状态。
    bind.execute(sa.text(
        '''UPDATE cooling_resources
           SET coordinate_system = NULL,
               coordinate_source = NULL,
               coordinate_verified_at = NULL'''
    ))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'cooling_resources' not in inspector.get_table_names():
        return
    columns = _cooling_columns(inspector)
    _validate_existing_columns(columns)
    # 只回到 0025 时保留健康回执列；跨越 0025 时先完成整条链预检。
    _preflight_downgrade(
        bind,
        inspector,
        include_lower_chain=not _targets_only_previous_revision(),
    )
    if 'coordinate_verified_at' in columns:
        op.drop_column('cooling_resources', 'coordinate_verified_at')
    if 'coordinate_source' in columns:
        op.drop_column('cooling_resources', 'coordinate_source')
    if 'coordinate_system' in columns:
        op.drop_column('cooling_resources', 'coordinate_system')
