"""增加 WxPusher UID 所有权验证与唯一约束

Revision ID: 0029_wxpusher_uid_ownership
Revises: 0028_authorized_community
Create Date: 2026-08-09 00:10:00.000000
"""

import importlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0029_wxpusher_uid_ownership'
down_revision = '0028_authorized_community'
branch_labels = None
depends_on = None


VERIFIED_COLUMN = 'wxpusher_uid_verified_at'
UNIQUE_NAME = 'uq_users_wxpusher_uid'
CHALLENGE_TABLE = 'wxpusher_binding_challenges'
CHALLENGE_INDEX_SPECS = {
    'ix_wxpusher_binding_challenges_user_active': [
        'user_id',
        'consumed_at',
        'revoked_at',
    ],
    'ix_wxpusher_binding_challenges_candidate_uid': ['candidate_uid'],
}
RESERVED_INDEX_TABLES = {
    UNIQUE_NAME: 'users',
    **{
        index_name: CHALLENGE_TABLE
        for index_name in CHALLENGE_INDEX_SPECS
    },
}


def _user_columns(inspector):
    return {column['name']: column for column in inspector.get_columns('users')}


def _index_has_predicate(index):
    for key, value in (index.get('dialect_options') or {}).items():
        if key.endswith('_where') and value is not None and str(value).strip():
            return True
    return False


def _validate_reserved_index_names(inspector, tables):
    """拒绝其他表占用本迁移的索引名，SQLite 中索引名全库共享。"""
    for table_name in sorted(tables):
        named_objects = list(inspector.get_indexes(table_name))
        named_objects.extend(inspector.get_unique_constraints(table_name))
        for item in named_objects:
            index_name = item.get('name')
            expected_table = RESERVED_INDEX_TABLES.get(index_name)
            if expected_table is not None and table_name != expected_table:
                raise RuntimeError(
                    'wxpusher ownership migration aborted: '
                    f'index_name_conflict={index_name}'
                )


def _validate_verified_column(columns, *, required):
    column = columns.get(VERIFIED_COLUMN)
    if column is None:
        if required:
            raise RuntimeError(
                'wxpusher ownership migration aborted: '
                f'missing_column={VERIFIED_COLUMN}'
            )
        return False
    if (
        not isinstance(column.get('type'), sa.DateTime)
        or column.get('nullable') is not True
    ):
        raise RuntimeError(
            'wxpusher ownership migration aborted: '
            f'invalid_column={VERIFIED_COLUMN}'
        )
    return True


def _normalized_server_default(column):
    value = str(column.get('default') or '').strip()
    value = value.split('::', 1)[0]
    return value.strip().strip("'\"() ")


def _validate_challenge_table(
    inspector,
    tables,
    *,
    required,
    require_indexes,
):
    if CHALLENGE_TABLE not in tables:
        if required:
            raise RuntimeError(
                'wxpusher ownership migration aborted: '
                f'missing_table={CHALLENGE_TABLE}'
            )
        return set()

    columns = {
        column['name']: column
        for column in inspector.get_columns(CHALLENGE_TABLE)
    }
    expected_names = {
        'id',
        'user_id',
        'candidate_uid',
        'code_hash',
        'created_at',
        'expires_at',
        'attempt_count',
        'consumed_at',
        'revoked_at',
    }
    invalid = set(columns) != expected_names

    id_column = columns.get('id')
    invalid = invalid or id_column is None or not isinstance(
        id_column.get('type'),
        sa.Integer,
    ) or not bool(id_column.get('primary_key'))

    specs = {
        'user_id': (sa.Integer, None, False),
        'candidate_uid': (sa.String, 80, False),
        'code_hash': (sa.String, 64, False),
        'created_at': (sa.DateTime, None, False),
        'expires_at': (sa.DateTime, None, False),
        'attempt_count': (sa.Integer, None, False),
        'consumed_at': (sa.DateTime, None, True),
        'revoked_at': (sa.DateTime, None, True),
    }
    for name, (expected_type, expected_length, expected_nullable) in specs.items():
        column = columns.get(name)
        if column is None:
            invalid = True
            continue
        column_type = column.get('type')
        if not isinstance(column_type, expected_type):
            invalid = True
        if (
            expected_length is not None
            and getattr(column_type, 'length', None) != expected_length
        ):
            invalid = True
        if column.get('nullable') is not expected_nullable:
            invalid = True
    if (
        columns.get('attempt_count') is not None
        and _normalized_server_default(columns['attempt_count']) != '0'
    ):
        invalid = True

    foreign_keys = inspector.get_foreign_keys(CHALLENGE_TABLE)
    valid_user_fks = [
        foreign_key
        for foreign_key in foreign_keys
        if list(foreign_key.get('constrained_columns') or []) == ['user_id']
        and foreign_key.get('referred_table') == 'users'
        and list(foreign_key.get('referred_columns') or []) == ['id']
        and str(
            (foreign_key.get('options') or {}).get('ondelete') or ''
        ).upper() == 'CASCADE'
    ]
    if len(foreign_keys) != 1 or len(valid_user_fks) != 1:
        invalid = True

    indexes = {
        index.get('name'): index
        for index in inspector.get_indexes(CHALLENGE_TABLE)
    }
    present_indexes = set()
    for index_name, expected_columns in CHALLENGE_INDEX_SPECS.items():
        index = indexes.get(index_name)
        if index is None:
            if require_indexes:
                invalid = True
            continue
        present_indexes.add(index_name)
        if (
            bool(index.get('unique'))
            or list(index.get('column_names') or []) != expected_columns
            or _index_has_predicate(index)
        ):
            invalid = True

    if invalid:
        raise RuntimeError(
            'wxpusher ownership migration aborted: invalid_challenge_table'
        )
    return present_indexes


def _begin_sqlite_write_transaction(bind):
    """SQLite 的 DDL 前显式开启真实写事务，使失败可整体回滚。"""
    if str(bind.dialect.name or '').lower() != 'sqlite':
        return
    driver_connection = getattr(
        getattr(bind, 'connection', None),
        'driver_connection',
        None,
    )
    if driver_connection is not None and getattr(
        driver_connection,
        'in_transaction',
        False,
    ):
        return
    bind.exec_driver_sql('BEGIN IMMEDIATE')


def _validate_user_unique_index(inspector):
    """同名对象必须是无谓词的单列唯一索引。"""
    named_constraints = [
        item
        for item in inspector.get_unique_constraints('users')
        if item.get('name') == UNIQUE_NAME
    ]
    named_indexes = [
        item
        for item in inspector.get_indexes('users')
        if item.get('name') == UNIQUE_NAME
    ]
    if named_constraints or len(named_indexes) > 1:
        raise RuntimeError(
            'wxpusher ownership migration aborted: '
            f'invalid_index={UNIQUE_NAME}'
        )
    if not named_indexes:
        return False
    index = named_indexes[0]
    if (
        not index.get('unique')
        or list(index.get('column_names') or []) != ['wxpusher_uid']
        or _index_has_predicate(index)
    ):
        raise RuntimeError(
            'wxpusher ownership migration aborted: '
            f'invalid_index={UNIQUE_NAME}'
        )
    return True


def _boolean_safe_statement(sql):
    return sa.text(sql).bindparams(sa.bindparam(
        'push_disabled',
        value=False,
        type_=sa.Boolean(),
    ))


def _duplicate_uid_revoke_statement():
    return _boolean_safe_statement(
        '''UPDATE users
           SET wxpusher_uid = NULL,
               wxpusher_uid_verified_at = NULL,
               push_enabled = :push_disabled,
               wxpusher_consent_version = NULL,
               wxpusher_consented_at = NULL
           WHERE wxpusher_uid IN (
               SELECT wxpusher_uid
               FROM users
               WHERE wxpusher_uid IS NOT NULL
               GROUP BY wxpusher_uid
               HAVING COUNT(*) > 1
           )'''
    )


def _revoke_all_unproven_statement():
    return _boolean_safe_statement(
        '''UPDATE users
           SET wxpusher_uid_verified_at = NULL,
               push_enabled = :push_disabled,
               wxpusher_consent_version = NULL,
               wxpusher_consented_at = NULL'''
    )


def _revoke_all_for_downgrade_statement():
    return _boolean_safe_statement(
        '''UPDATE users
           SET push_enabled = :push_disabled,
               wxpusher_uid = NULL,
               wxpusher_consent_version = NULL,
               wxpusher_consented_at = NULL'''
    )


def _preflight_lower_downgrade(bind, inspector):
    """只预检实际会回退的迁移，禁止本迁移先撤权再失败。"""
    context = op.get_context()
    environment_context = getattr(context, 'environment_context', None)
    if environment_context is None:
        return
    destination = environment_context.get_revision_argument()
    try:
        planned = tuple(environment_context.script.iterate_revisions(
            context.get_current_heads(),
            destination,
            select_for_downgrade=True,
        ))
    except Exception as exc:
        raise RuntimeError(
            'wxpusher ownership migration aborted: '
            'unable_to_resolve_downgrade_plan'
        ) from exc
    planned_ids = {item.revision for item in planned}

    if '0028_authorized_community' in planned_ids:
        migration_0028 = importlib.import_module(
            'migrations.versions.0028_authorized_community'
        )
        migration_0028._preflight_downgrade(bind, inspector)

    if '0027_cross_platform_identity' in planned_ids:
        migration_0027 = importlib.import_module(
            'migrations.versions.0027_cross_platform_identity'
        )
        migration_0027._preflight_downgrade(
            bind,
            inspector,
            include_lower_chain=False,
        )

    if '0026_cooling_coordinate_verify' in planned_ids:
        migration_0026 = importlib.import_module(
            'migrations.versions.0026_cooling_coordinate_verification'
        )
        migration_0026._preflight_downgrade(
            bind,
            inspector,
            include_lower_chain=False,
        )

    if '0025_health_sensitive_consent' in planned_ids:
        migration_0025 = importlib.import_module(
            'migrations.versions.0025_health_sensitive_consent'
        )
        # 0025 的预检会按同一降级计划继续检查 0023。
        migration_0025._preflight_downgrade(bind, inspector)


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if 'users' not in tables:
        raise RuntimeError(
            "wxpusher ownership migration aborted: missing_tables=['users']"
        )
    _validate_reserved_index_names(inspector, tables)

    columns = _user_columns(inspector)
    required = {
        'wxpusher_uid',
        'push_enabled',
        'wxpusher_consent_version',
        'wxpusher_consented_at',
    }
    missing = sorted(required - set(columns))
    if missing:
        raise RuntimeError(
            f'wxpusher ownership migration aborted: missing_columns={missing}'
        )
    verified_column_exists = _validate_verified_column(
        columns,
        required=False,
    )
    unique_index_exists = _validate_user_unique_index(inspector)
    challenge_indexes = _validate_challenge_table(
        inspector,
        tables,
        required=False,
        require_indexes=False,
    )

    _begin_sqlite_write_transaction(bind)

    if not verified_column_exists:
        op.add_column(
            'users',
            sa.Column(VERIFIED_COLUMN, sa.DateTime(), nullable=True),
        )

    # 先与应用层的 strip 语义对齐，避免空格绕过重复所有者检查。
    bind.execute(sa.text(
        '''UPDATE users
           SET wxpusher_uid = TRIM(wxpusher_uid)
           WHERE wxpusher_uid IS NOT NULL'''
    ))
    bind.execute(sa.text(
        "UPDATE users SET wxpusher_uid = NULL WHERE wxpusher_uid = ''"
    ))
    # 重复历史 UID 无法判断真实所有者，整组撤权。
    bind.execute(_duplicate_uid_revoke_statement())
    # 既有 UID 没有所有权证明，统一要求重新验证和重新同意。
    bind.execute(_revoke_all_unproven_statement())

    if not unique_index_exists:
        # SQLite 重建 users 会触发现有外键约束，唯一索引可原地建立且强度相同。
        op.create_index(
            UNIQUE_NAME,
            'users',
            ['wxpusher_uid'],
            unique=True,
        )

    if CHALLENGE_TABLE not in tables:
        op.create_table(
            CHALLENGE_TABLE,
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'user_id',
                sa.Integer(),
                sa.ForeignKey('users.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('candidate_uid', sa.String(length=80), nullable=False),
            sa.Column('code_hash', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('consumed_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
        )
    for index_name, column_names in CHALLENGE_INDEX_SPECS.items():
        if index_name in challenge_indexes:
            continue
        op.create_index(
            index_name,
            CHALLENGE_TABLE,
            column_names,
            unique=False,
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if 'users' not in tables:
        raise RuntimeError(
            "wxpusher ownership migration aborted: missing_tables=['users']"
        )
    _validate_reserved_index_names(inspector, tables)
    columns = _user_columns(inspector)
    _validate_verified_column(columns, required=True)
    unique_index_exists = _validate_user_unique_index(inspector)
    if not unique_index_exists:
        raise RuntimeError(
            'wxpusher ownership migration aborted: '
            f'missing_index={UNIQUE_NAME}'
        )
    _validate_challenge_table(
        inspector,
        tables,
        required=True,
        require_indexes=True,
    )
    _preflight_lower_downgrade(bind, inspector)
    _begin_sqlite_write_transaction(bind)

    # 旧发送端不会检查所有权时间，回滚前先撤销全部推送授权。
    bind.execute(_revoke_all_for_downgrade_statement())
    if CHALLENGE_TABLE in tables:
        op.drop_table(CHALLENGE_TABLE)
    if unique_index_exists:
        op.drop_index(UNIQUE_NAME, table_name='users')
    op.drop_column('users', VERIFIED_COLUMN)
