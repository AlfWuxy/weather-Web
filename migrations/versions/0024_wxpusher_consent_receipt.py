"""为 WxPusher 独立同意增加可审计回执

Revision ID: 0024_wxpusher_consent_receipt
Revises: 0023_auth_session_version
Create Date: 2026-07-18 19:15:00.000000
"""

import importlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0024_wxpusher_consent_receipt'
down_revision = '0023_auth_session_version'
branch_labels = None
depends_on = None


CONSENT_VERSION_COLUMN = 'wxpusher_consent_version'
CONSENT_TIME_COLUMN = 'wxpusher_consented_at'
AUTH_SESSION_REVISION = '0023_auth_session_version'


def _user_columns(inspector):
    return {
        column['name']: column
        for column in inspector.get_columns('users')
    }


def _validate_existing_columns(columns):
    """拒绝把错误的同名列当作已完成迁移。"""
    invalid_columns = []
    version_column = columns.get(CONSENT_VERSION_COLUMN)
    if version_column is not None:
        column_type = version_column.get('type')
        if (
            not isinstance(column_type, sa.String)
            or getattr(column_type, 'length', None) != 64
            or version_column.get('nullable') is not True
        ):
            invalid_columns.append(CONSENT_VERSION_COLUMN)

    time_column = columns.get(CONSENT_TIME_COLUMN)
    if time_column is not None:
        if (
            not isinstance(time_column.get('type'), sa.DateTime)
            or time_column.get('nullable') is not True
        ):
            invalid_columns.append(CONSENT_TIME_COLUMN)

    if invalid_columns:
        raise RuntimeError(
            'wxpusher consent migration aborted: invalid_columns='
            f'{sorted(invalid_columns)}; revision was not advanced'
        )


def _preflight_lower_downgrade(bind, inspector):
    """如果命令还要越过 0023，先执行其全链丢数据检查。"""
    context = op.get_context()
    environment_context = getattr(context, 'environment_context', None)
    if environment_context is None:
        return
    destination = environment_context.get_revision_argument()
    try:
        planned_revisions = tuple(environment_context.script.iterate_revisions(
            context.get_current_heads(),
            destination,
            select_for_downgrade=True,
        ))
    except Exception as exc:
        raise RuntimeError(
            'wxpusher consent migration aborted: '
            'unable_to_resolve_downgrade_plan'
        ) from exc
    if AUTH_SESSION_REVISION not in {
        planned.revision for planned in planned_revisions
    }:
        return
    auth_session_migration = importlib.import_module(
        'migrations.versions.0023_auth_session_version'
    )
    auth_session_migration._preflight_downgrade(bind, inspector)


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'users' not in inspector.get_table_names():
        raise RuntimeError(
            'wxpusher consent migration aborted: missing_tables=[\'users\']'
        )

    columns = _user_columns(inspector)
    if 'push_enabled' not in columns:
        raise RuntimeError(
            'wxpusher consent migration aborted: '
            'missing_columns=[\'users.push_enabled\']'
        )
    _validate_existing_columns(columns)

    if CONSENT_VERSION_COLUMN not in columns:
        op.add_column(
            'users',
            sa.Column(CONSENT_VERSION_COLUMN, sa.String(length=64), nullable=True),
        )
    if CONSENT_TIME_COLUMN not in columns:
        op.add_column(
            'users',
            sa.Column(CONSENT_TIME_COLUMN, sa.DateTime(), nullable=True),
        )

    # 历史开关没有可信的版本和时间证据，上线后统一重新开启。
    bind.execute(sa.text(
        '''UPDATE users
           SET push_enabled = 0,
               wxpusher_consent_version = NULL,
               wxpusher_consented_at = NULL'''
    ))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'users' not in inspector.get_table_names():
        return
    columns = _user_columns(inspector)
    # SQLite DDL 不可回滚，所有下游降级检查必须先于本迁移的任何修改。
    _preflight_lower_downgrade(bind, inspector)

    # 旧代码无法校验回执，降级前先失效所有推送授权。
    if 'push_enabled' in columns:
        bind.execute(sa.text('UPDATE users SET push_enabled = 0'))
    if CONSENT_TIME_COLUMN in columns:
        op.drop_column('users', CONSENT_TIME_COLUMN)
    if CONSENT_VERSION_COLUMN in columns:
        op.drop_column('users', CONSENT_VERSION_COLUMN)
