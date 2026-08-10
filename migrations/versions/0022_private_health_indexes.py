"""为小程序私有健康查询增加复合索引

Revision ID: 0022_private_health_indexes
Revises: 0021_debrief_downgrade_guard
Create Date: 2026-07-18 17:30:00.000000
"""

from alembic import op
from sqlalchemy import inspect


revision = '0022_private_health_indexes'
down_revision = '0021_debrief_downgrade_guard'
branch_labels = None
depends_on = None


INDEX_SPECS = (
    (
        'health_diary',
        'ix_health_diary_owner_member_date_id',
        ('user_id', 'member_id', 'entry_date', 'id'),
    ),
    (
        'medication_reminders',
        'ix_medication_owner_member_id',
        ('user_id', 'member_id', 'id'),
    ),
    (
        'health_risk_assessments',
        'ix_assessment_owner_member_date_id',
        ('user_id', 'member_id', 'assessment_date', 'id'),
    ),
)


def _table_indexes(inspector, table_name):
    """返回当前索引结构，兼容 SQLite 和 PostgreSQL 反射结果。"""
    return {
        item.get('name'): item
        for item in inspector.get_indexes(table_name)
        if item.get('name')
    }


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    # 先完成全部结构预检，再创建任何索引，避免迁移中途留下半成品。
    missing_tables = sorted(
        table_name
        for table_name, _index_name, _columns in INDEX_SPECS
        if table_name not in table_names
    )
    if missing_tables:
        raise RuntimeError(
            'private health index migration aborted: missing_tables='
            f'{missing_tables}'
        )

    missing_columns_by_table = {}
    for table_name, index_name, columns in INDEX_SPECS:
        available_columns = {
            column['name']
            for column in inspector.get_columns(table_name)
        }
        missing_columns = sorted(set(columns) - available_columns)
        if missing_columns:
            missing_columns_by_table[table_name] = missing_columns

    if missing_columns_by_table:
        raise RuntimeError(
            'private health index migration aborted: missing_columns='
            f'{missing_columns_by_table}'
        )

    invalid_indexes = {}
    for table_name, index_name, columns in INDEX_SPECS:
        existing = _table_indexes(inspector, table_name).get(index_name)
        if existing is None:
            continue
        actual_columns = existing.get('column_names') or []
        if actual_columns != list(columns) or bool(existing.get('unique')):
            invalid_indexes[index_name] = {
                'columns': actual_columns,
                'unique': bool(existing.get('unique')),
            }
    if invalid_indexes:
        raise RuntimeError(
            'private health index migration aborted: invalid_indexes='
            f'{invalid_indexes}'
        )

    for table_name, index_name, columns in INDEX_SPECS:
        if index_name in _table_indexes(inspector, table_name):
            continue
        op.create_index(index_name, table_name, list(columns), unique=False)


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    for table_name, index_name, _columns in reversed(INDEX_SPECS):
        if table_name not in table_names:
            continue
        if index_name not in _table_indexes(inspector, table_name):
            continue
        op.drop_index(index_name, table_name=table_name)
