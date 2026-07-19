"""为健康敏感信息增加独立同意回执

Revision ID: 0025_health_sensitive_consent
Revises: 0024_wxpusher_consent_receipt
Create Date: 2026-07-19 13:30:00.000000
"""

import importlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0025_health_sensitive_consent'
down_revision = '0024_wxpusher_consent_receipt'
branch_labels = None
depends_on = None


CONSENT_VERSION_COLUMN = 'health_sensitive_consent_version'
CONSENT_TIME_COLUMN = 'health_sensitive_consented_at'


def _user_columns(inspector):
    return {
        column['name']: column
        for column in inspector.get_columns('users')
    }


def _validate_existing_columns(columns):
    """拒绝把错误的同名列当作可信回执结构。"""
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
            'health sensitive consent migration aborted: invalid_columns='
            f'{sorted(invalid_columns)}; revision was not advanced'
        )


def _preflight_lower_downgrade():
    """跨越旧迁移时先执行其整条降级链保护。"""
    previous_migration = importlib.import_module(
        'migrations.versions.0024_wxpusher_consent_receipt'
    )
    previous_migration._preflight_lower_downgrade(op.get_bind(), inspect(op.get_bind()))


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'users' not in inspector.get_table_names():
        raise RuntimeError(
            "health sensitive consent migration aborted: missing_tables=['users']"
        )

    columns = _user_columns(inspector)
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

    # 一般隐私同意没有健康敏感信息授权效力，历史账号必须重新明确确认。
    bind.execute(sa.text(
        '''UPDATE users
           SET health_sensitive_consent_version = NULL,
               health_sensitive_consented_at = NULL'''
    ))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if 'users' not in inspector.get_table_names():
        return
    columns = _user_columns(inspector)
    _validate_existing_columns(columns)
    _preflight_lower_downgrade()

    present_columns = {
        CONSENT_VERSION_COLUMN,
        CONSENT_TIME_COLUMN,
    } & set(columns)
    if present_columns:
        predicates = []
        if CONSENT_VERSION_COLUMN in columns:
            predicates.append(f'{CONSENT_VERSION_COLUMN} IS NOT NULL')
        if CONSENT_TIME_COLUMN in columns:
            predicates.append(f'{CONSENT_TIME_COLUMN} IS NOT NULL')
        receipt_count = bind.execute(sa.text(
            f'''SELECT COUNT(*) FROM users
                WHERE {' OR '.join(predicates)}'''
        )).scalar_one()
        if receipt_count:
            raise RuntimeError(
                'health sensitive consent downgrade aborted: '
                f'nonempty_receipt_count={receipt_count}'
            )

    if CONSENT_TIME_COLUMN in columns:
        op.drop_column('users', CONSENT_TIME_COLUMN)
    if CONSENT_VERSION_COLUMN in columns:
        op.drop_column('users', CONSENT_VERSION_COLUMN)
