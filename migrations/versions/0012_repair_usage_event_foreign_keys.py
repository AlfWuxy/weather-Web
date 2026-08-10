"""匿名化历史埋点中的失效外键

Revision ID: 0012_repair_usage_event_fks
Revises: 0011_miniprogram_runtime
Create Date: 2026-07-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0012_repair_usage_event_fks'
down_revision = '0011_miniprogram_runtime'
branch_labels = None
depends_on = None


def _table_exists(inspector, name):
    return name in inspector.get_table_names()


USAGE_EVENT_PARENTS = (
    ('user_id', 'users'),
    ('pair_id', 'pairs'),
    ('member_id', 'family_members'),
)


def _constraint_name(column_name, parent_table):
    return f'fk_usage_events_{column_name}_{parent_table}'


def _clear_orphaned_reference(bind, column_name, parent_table):
    """保留匿名计数，并清空失效主体引用及可能再识别的元数据。"""
    bind.execute(
        sa.text(
            f'''
            UPDATE usage_events
            SET {column_name} = NULL,
                meta_json = NULL
            WHERE {column_name} IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM {parent_table}
                  WHERE {parent_table}.id = usage_events.{column_name}
              )
            '''
        )
    )


def _replace_foreign_keys(bind, ondelete=None):
    """跨 SQLite 与 PostgreSQL 统一埋点父级删除语义。"""
    inspector = inspect(bind)
    existing_by_column = {
        tuple(foreign_key.get('constrained_columns') or ()): foreign_key
        for foreign_key in inspector.get_foreign_keys('usage_events')
    }

    if bind.dialect.name == 'sqlite':
        naming_convention = {
            'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s'
        }
        with op.batch_alter_table(
            'usage_events',
            recreate='always',
            naming_convention=naming_convention,
        ) as batch_op:
            for column_name, parent_table in USAGE_EVENT_PARENTS:
                foreign_key = existing_by_column.get((column_name,))
                if foreign_key is not None:
                    batch_op.drop_constraint(
                        foreign_key.get('name')
                        or _constraint_name(column_name, parent_table),
                        type_='foreignkey',
                    )
                options = {'ondelete': ondelete} if ondelete else {}
                batch_op.create_foreign_key(
                    _constraint_name(column_name, parent_table),
                    parent_table,
                    [column_name],
                    ['id'],
                    **options,
                )
        return

    for column_name, parent_table in USAGE_EVENT_PARENTS:
        foreign_key = existing_by_column.get((column_name,))
        if foreign_key is not None:
            op.drop_constraint(
                foreign_key['name'],
                'usage_events',
                type_='foreignkey',
            )
        options = {'ondelete': ondelete} if ondelete else {}
        op.create_foreign_key(
            _constraint_name(column_name, parent_table),
            'usage_events',
            parent_table,
            [column_name],
            ['id'],
            **options,
        )


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if not _table_exists(inspector, 'usage_events'):
        return

    # 上架版本建立新的匿名埋点边界，旧自由元数据不进入后续分析口径。
    bind.execute(
        sa.text(
            '''
            UPDATE usage_events
            SET meta_json = NULL
            WHERE meta_json IS NOT NULL
            '''
        )
    )
    for column_name, parent_table in USAGE_EVENT_PARENTS:
        if _table_exists(inspector, parent_table):
            _clear_orphaned_reference(bind, column_name, parent_table)
    _replace_foreign_keys(bind, ondelete='SET NULL')


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if _table_exists(inspector, 'usage_events'):
        _replace_foreign_keys(bind)
    # 历史标识和旧自由元数据无法可靠恢复，降级继续保留匿名计数。
