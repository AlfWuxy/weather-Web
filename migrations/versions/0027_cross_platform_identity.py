"""增加待验证手机号标识与一次性小程序绑定挑战

Revision ID: 0027_cross_platform_identity
Revises: 0026_cooling_coordinate_verify
Create Date: 2026-07-30 18:40:00.000000
"""

import importlib
import re

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = '0027_cross_platform_identity'
down_revision = '0026_cooling_coordinate_verify'
branch_labels = None
depends_on = None


USER_COLUMNS = {
    'account_origin': (sa.String, 32, False),
    'phone_normalized': (sa.String, 32, True),
    'phone_verified_at': (sa.DateTime, None, True),
}
IDENTITY_COLUMNS = {
    'binding_auth_version': (sa.Integer, None, False),
    'link_failed_count': (sa.Integer, None, False),
    'link_first_failed_at': (sa.DateTime, None, True),
    'link_locked_until': (sa.DateTime, None, True),
}
CHALLENGE_REQUIRED_COLUMNS = {
    'id',
    'user_id',
    'code_hash',
    'created_at',
    'expires_at',
    'auth_version_at_create',
    'consumed_at',
    'consumed_identity_id',
    'revoked_at',
    'attempt_count',
}
PHONE_USERNAME_RE = re.compile(
    r'(?:1[3-9][0-9]{9}|861[3-9][0-9]{9}|00861[3-9][0-9]{9})',
    re.ASCII,
)
SUPPORTED_PARTIAL_INDEX_DIALECTS = {'sqlite', 'postgresql'}
VERIFIED_PHONE_INDEX_PREDICATE = 'phone_verified_at IS NOT NULL'
ACTIVE_CHALLENGE_INDEX_PREDICATE = (
    'consumed_at IS NULL AND revoked_at IS NULL'
)
LEGACY_PLACEHOLDER_USER_COLUMNS = {
    'id',
    'username',
    'role',
    'auth_version',
    'deleted_at',
    'email',
    'last_login',
    'age',
    'gender',
    'community',
    'has_chronic_disease',
    'chronic_diseases',
    'wxpusher_uid',
    'push_enabled',
    'wxpusher_consent_version',
    'wxpusher_consented_at',
    'health_sensitive_consent_version',
    'health_sensitive_consented_at',
}
LEGACY_PRIVATE_OWNER_COLUMNS = (
    ('family_members', 'user_id'),
    ('health_diary', 'user_id'),
    ('medication_reminders', 'user_id'),
    ('health_risk_assessments', 'user_id'),
    ('notifications', 'user_id'),
    ('pair_links', 'caregiver_id'),
    ('pairs', 'caregiver_id'),
    ('debriefs', 'owner_user_id'),
    ('api_tokens', 'user_id'),
    ('alert_deliveries', 'user_id'),
    ('alert_deliveries', 'reviewed_by_user_id'),
)


def _columns(inspector, table_name):
    return {
        column['name']: column
        for column in inspector.get_columns(table_name)
    }


def _require_partial_index_dialect(bind):
    """部分唯一索引只在已验证过语义的数据库方言上执行。"""
    dialect_name = str(bind.dialect.name or '').lower()
    if dialect_name not in SUPPORTED_PARTIAL_INDEX_DIALECTS:
        raise RuntimeError(
            'cross platform identity migration aborted: '
            f'unsupported_partial_index_dialect={dialect_name or "unknown"}'
        )
    return dialect_name


def _validate_columns(columns, specs, label):
    """拒绝把类型或可空性错误的同名列当作完成迁移。"""
    invalid = []
    for name, (expected_type, expected_length, expected_nullable) in specs.items():
        column = columns.get(name)
        if column is None:
            continue
        column_type = column.get('type')
        is_invalid = not isinstance(column_type, expected_type)
        if expected_length is not None:
            is_invalid = (
                is_invalid
                or getattr(column_type, 'length', None) != expected_length
            )
        if column.get('nullable') is not expected_nullable:
            is_invalid = True
        if is_invalid:
            invalid.append(name)
    if invalid:
        raise RuntimeError(
            'cross platform identity migration aborted: '
            f'invalid_{label}_columns={sorted(invalid)}'
        )


def _validate_phone_username_namespace(bind):
    """手机号和用户名共用登录输入框，历史账号不得占用手机号命名空间。"""
    conflicts = [
        int(row.id)
        for row in bind.execute(
            sa.text('SELECT id, username FROM users')
        )
        if PHONE_USERNAME_RE.fullmatch(str(row.username or '').strip())
    ]
    if conflicts:
        raise RuntimeError(
            'cross platform identity migration aborted: '
            f'phone_shaped_username_ids={conflicts[:20]}'
        )


def _validate_account_origins(bind, user_columns):
    """已有来源列必须是服务端支持的固定枚举。"""
    if 'account_origin' not in user_columns:
        return
    invalid = bind.execute(
        sa.text(
            '''SELECT id FROM users
               WHERE account_origin IS NULL
                  OR account_origin NOT IN (
                      'web',
                      'miniprogram_placeholder',
                      'retired_miniprogram'
                  )
               LIMIT 20'''
        )
    ).all()
    if invalid:
        raise RuntimeError(
            'cross platform identity migration aborted: '
            f'invalid_account_origin_ids={[int(row.id) for row in invalid]}'
        )


def _legacy_miniprogram_placeholder_user_ids(
    bind,
    inspector,
    user_columns,
):
    """只回填能由旧版生成规则和空白数据共同证明的微信占位账号。"""
    if not LEGACY_PLACEHOLDER_USER_COLUMNS <= set(user_columns):
        return []

    rows = bind.execute(sa.text(
        '''SELECT
               users.id,
               users.username,
               users.role,
               users.auth_version,
               users.deleted_at,
               users.email,
               users.last_login,
               users.age,
               users.gender,
               users.community,
               users.has_chronic_disease,
               users.chronic_diseases,
               users.wxpusher_uid,
               users.push_enabled,
               users.wxpusher_consent_version,
               users.wxpusher_consented_at,
               users.health_sensitive_consent_version,
               users.health_sensitive_consented_at,
               miniprogram_identities.openid_hash
           FROM users
           JOIN miniprogram_identities
             ON miniprogram_identities.user_id = users.id'''
    )).mappings().all()
    tables = set(inspector.get_table_names())
    table_columns = {
        table_name: set(_columns(inspector, table_name))
        for table_name, _column_name in LEGACY_PRIVATE_OWNER_COLUMNS
        if table_name in tables
    }
    candidates = []
    for row in rows:
        openid_hash = str(row['openid_hash'] or '').strip().lower()
        if not re.fullmatch(r'[0-9a-f]{64}', openid_hash):
            continue
        base_username = f'wx_{openid_hash[:24]}'
        username = str(row['username'] or '').strip().casefold()
        if re.fullmatch(
            rf'{re.escape(base_username)}(?:_[1-9][0-9]*)?',
            username,
        ) is None:
            continue
        if (
            row['role'] != 'user'
            or int(row['auth_version'] or 0) != 1
            or row['deleted_at'] is not None
            or row['last_login'] is not None
            or row['age'] is not None
            or bool(row['has_chronic_disease'])
            or bool(row['push_enabled'])
            or any(
                row[name]
                for name in (
                    'email',
                    'gender',
                    'community',
                    'chronic_diseases',
                    'wxpusher_uid',
                    'wxpusher_consent_version',
                    'wxpusher_consented_at',
                    'health_sensitive_consent_version',
                    'health_sensitive_consented_at',
                )
            )
        ):
            continue
        if any(
            row[name] is not None
            for name in ('phone_normalized', 'phone_verified_at')
            if name in row
        ):
            continue

        owns_private_data = False
        for table_name, column_name in LEGACY_PRIVATE_OWNER_COLUMNS:
            if table_name not in tables:
                continue
            if column_name not in table_columns[table_name]:
                owns_private_data = True
                break
            count = bind.execute(
                sa.text(
                    f'''SELECT COUNT(*) FROM {table_name}
                        WHERE {column_name} = :user_id'''
                ),
                {'user_id': int(row['id'])},
            ).scalar_one()
            if count:
                owns_private_data = True
                break
        if not owns_private_data:
            candidates.append(int(row['id']))
    return candidates


def _validate_index(
    inspector,
    table_name,
    name,
    columns,
    *,
    expected_unique,
    expected_where=None,
    dialect_name=None,
):
    indexes = {item['name']: item for item in inspector.get_indexes(table_name)}
    existing = indexes.get(name)
    if existing is None:
        return False
    if (
        bool(existing.get('unique')) != bool(expected_unique)
        or list(existing.get('column_names') or []) != list(columns)
    ):
        raise RuntimeError(
            'cross platform identity migration aborted: '
            f'invalid_index={name}'
        )
    if expected_where is not None:
        if not _index_predicate_matches(
            existing,
            expected_where=expected_where,
            dialect_name=dialect_name,
        ):
            raise RuntimeError(
                'cross platform identity migration aborted: '
                f'invalid_index_predicate={name}'
            )
    return True


def _index_predicate_matches(
    index,
    *,
    expected_where,
    dialect_name,
):
    """比较反射到的部分索引谓词，忽略方言生成的空白和引号。"""
    option_name = f'{dialect_name}_where'
    predicate = index.get('dialect_options', {}).get(option_name)
    normalized_predicate = re.sub(
        r'[\s()"`]+',
        '',
        str('' if predicate is None else predicate).lower(),
    )
    normalized_expected = re.sub(
        r'[\s()"`]+',
        '',
        str(expected_where).lower(),
    )
    return normalized_predicate == normalized_expected


def _targets_only_previous_revision():
    try:
        target = context.get_revision_argument()
    except Exception:
        return False
    if isinstance(target, tuple):
        return len(target) == 1 and target[0] == down_revision
    return target in {down_revision, '-1'}


def _preflight_upgrade(bind, inspector):
    """在 SQLite 的首个 DDL 前完成全部可预见冲突检查。"""
    dialect_name = _require_partial_index_dialect(bind)

    tables = set(inspector.get_table_names())
    required = {'users', 'miniprogram_identities'}
    missing = sorted(required - tables)
    if missing:
        raise RuntimeError(
            'cross platform identity migration aborted: '
            f'missing_tables={missing}'
        )

    _validate_phone_username_namespace(bind)
    user_columns = _columns(inspector, 'users')
    identity_columns = _columns(inspector, 'miniprogram_identities')
    _validate_columns(user_columns, USER_COLUMNS, 'user')
    _validate_columns(identity_columns, IDENTITY_COLUMNS, 'identity')
    _validate_account_origins(bind, user_columns)
    _validate_index(
        inspector,
        'users',
        'uq_users_phone_normalized',
        ['phone_normalized'],
        expected_unique=True,
    )
    _validate_index(
        inspector,
        'users',
        'ix_users_phone_normalized',
        ['phone_normalized'],
        expected_unique=False,
    )
    _validate_index(
        inspector,
        'users',
        'uq_users_verified_phone_normalized',
        ['phone_normalized'],
        expected_unique=True,
        expected_where=VERIFIED_PHONE_INDEX_PREDICATE,
        dialect_name=dialect_name,
    )
    _validate_index(
        inspector,
        'miniprogram_identities',
        'uq_miniprogram_identities_user_id',
        ['user_id'],
        expected_unique=True,
    )

    duplicate_identity = bind.execute(sa.text(
        '''SELECT user_id, COUNT(*) AS identity_count
           FROM miniprogram_identities
           GROUP BY user_id
           HAVING COUNT(*) > 1
           LIMIT 1'''
    )).first()
    if duplicate_identity is not None:
        raise RuntimeError(
            'cross platform identity migration aborted: '
            f'duplicate_identity_user_id={int(duplicate_identity.user_id)}'
        )

    if {'phone_normalized', 'phone_verified_at'} <= set(user_columns):
        duplicate_verified_phone = bind.execute(sa.text(
            '''SELECT phone_normalized, COUNT(*) AS phone_count
               FROM users
               WHERE phone_normalized IS NOT NULL
                 AND phone_verified_at IS NOT NULL
               GROUP BY phone_normalized
               HAVING COUNT(*) > 1
               LIMIT 1'''
        )).first()
        if duplicate_verified_phone is not None:
            raise RuntimeError(
                'cross platform identity migration aborted: '
                'duplicate_verified_phone_count='
                f'{int(duplicate_verified_phone.phone_count)}'
            )

    if 'miniprogram_link_challenges' not in tables:
        return
    challenge_columns = _columns(
        inspector,
        'miniprogram_link_challenges',
    )
    missing_challenge_columns = (
        CHALLENGE_REQUIRED_COLUMNS - set(challenge_columns)
    )
    if missing_challenge_columns not in (
        set(),
        {'auth_version_at_create'},
    ):
        raise RuntimeError(
            'cross platform identity migration aborted: '
            'invalid_challenge_columns='
            f'{sorted(missing_challenge_columns)}'
        )

    challenge_indexes = {
        item['name']: item
        for item in inspector.get_indexes('miniprogram_link_challenges')
    }
    ordinary_indexes = {
        'ix_mp_link_challenges_user_id': ['user_id'],
        'ix_mp_link_challenges_code_hash': ['code_hash'],
        'ix_mp_link_challenges_expires_at': ['expires_at'],
    }
    for name, columns in ordinary_indexes.items():
        existing = challenge_indexes.get(name)
        if existing is not None and (
            existing.get('unique')
            or list(existing.get('column_names') or []) != columns
        ):
            raise RuntimeError(
                'cross platform identity migration aborted: '
                f'invalid_index={name}'
            )
    active_index = challenge_indexes.get('uq_mp_link_active_user')
    if active_index is not None and (
        not active_index.get('unique')
        or list(active_index.get('column_names') or []) != ['user_id']
    ):
        raise RuntimeError(
            'cross platform identity migration aborted: '
            'invalid_index=uq_mp_link_active_user'
        )
    duplicate_active_challenge = bind.execute(sa.text(
        '''SELECT user_id, COUNT(*) AS challenge_count
           FROM miniprogram_link_challenges
           WHERE consumed_at IS NULL AND revoked_at IS NULL
           GROUP BY user_id
           HAVING COUNT(*) > 1
           LIMIT 1'''
    )).first()
    if duplicate_active_challenge is not None:
        raise RuntimeError(
            'cross platform identity migration aborted: '
            'duplicate_active_challenge_user_id='
            f'{int(duplicate_active_challenge.user_id)}'
        )


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    _preflight_upgrade(bind, inspector)

    user_columns = _columns(inspector, 'users')
    _validate_columns(user_columns, USER_COLUMNS, 'user')
    legacy_placeholder_user_ids = []
    if 'account_origin' not in user_columns:
        legacy_placeholder_user_ids = (
            _legacy_miniprogram_placeholder_user_ids(
                bind,
                inspector,
                user_columns,
            )
        )
        op.add_column(
            'users',
            sa.Column(
                'account_origin',
                sa.String(length=32),
                nullable=False,
                server_default='web',
            ),
        )
        if legacy_placeholder_user_ids:
            bind.execute(
                sa.text(
                    '''UPDATE users
                       SET account_origin = 'miniprogram_placeholder'
                       WHERE id IN :user_ids'''
                ).bindparams(
                    sa.bindparam('user_ids', expanding=True)
                ),
                {'user_ids': legacy_placeholder_user_ids},
            )
    if 'phone_normalized' not in user_columns:
        op.add_column(
            'users',
            sa.Column('phone_normalized', sa.String(length=32), nullable=True),
        )
    if 'phone_verified_at' not in user_columns:
        op.add_column(
            'users',
            sa.Column('phone_verified_at', sa.DateTime(), nullable=True),
        )

    # 早期候选版可能留下唯一索引，升级时先移除，避免待验证手机号抢占。
    user_indexes = {
        item['name']
        for item in inspect(bind).get_indexes('users')
    }
    if 'uq_users_phone_normalized' in user_indexes:
        op.drop_index('uq_users_phone_normalized', table_name='users')

    inspector = inspect(bind)
    if not _validate_index(
        inspector,
        'users',
        'ix_users_phone_normalized',
        ['phone_normalized'],
        expected_unique=False,
    ):
        op.create_index(
            'ix_users_phone_normalized',
            'users',
            ['phone_normalized'],
            unique=False,
        )
    inspector = inspect(bind)
    if not _validate_index(
        inspector,
        'users',
        'uq_users_verified_phone_normalized',
        ['phone_normalized'],
        expected_unique=True,
        expected_where=VERIFIED_PHONE_INDEX_PREDICATE,
        dialect_name=bind.dialect.name,
    ):
        op.create_index(
            'uq_users_verified_phone_normalized',
            'users',
            ['phone_normalized'],
            unique=True,
            sqlite_where=sa.text(VERIFIED_PHONE_INDEX_PREDICATE),
            postgresql_where=sa.text(VERIFIED_PHONE_INDEX_PREDICATE),
        )

    identity_columns = _columns(inspect(bind), 'miniprogram_identities')
    _validate_columns(identity_columns, IDENTITY_COLUMNS, 'identity')
    binding_auth_version_added = (
        'binding_auth_version' not in identity_columns
    )
    if binding_auth_version_added:
        op.add_column(
            'miniprogram_identities',
            sa.Column(
                'binding_auth_version',
                sa.Integer(),
                nullable=False,
                server_default='1',
            ),
        )
        op.execute(sa.text(
            '''UPDATE miniprogram_identities
               SET binding_auth_version = (
                   SELECT users.auth_version
                   FROM users
                   WHERE users.id = miniprogram_identities.user_id
               )'''
        ))
    if 'link_failed_count' not in identity_columns:
        op.add_column(
            'miniprogram_identities',
            sa.Column(
                'link_failed_count',
                sa.Integer(),
                nullable=False,
                server_default='0',
            ),
        )
    if 'link_first_failed_at' not in identity_columns:
        op.add_column(
            'miniprogram_identities',
            sa.Column('link_first_failed_at', sa.DateTime(), nullable=True),
        )
    if 'link_locked_until' not in identity_columns:
        op.add_column(
            'miniprogram_identities',
            sa.Column('link_locked_until', sa.DateTime(), nullable=True),
        )

    inspector = inspect(bind)
    if not _validate_index(
        inspector,
        'miniprogram_identities',
        'uq_miniprogram_identities_user_id',
        ['user_id'],
        expected_unique=True,
    ):
        op.create_index(
            'uq_miniprogram_identities_user_id',
            'miniprogram_identities',
            ['user_id'],
            unique=True,
        )

    tables = set(inspect(bind).get_table_names())
    if 'miniprogram_link_challenges' not in tables:
        op.create_table(
            'miniprogram_link_challenges',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('code_hash', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column(
                'auth_version_at_create',
                sa.Integer(),
                nullable=False,
                server_default='1',
            ),
            sa.Column('consumed_at', sa.DateTime(), nullable=True),
            sa.Column('consumed_identity_id', sa.Integer(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column(
                'attempt_count',
                sa.Integer(),
                nullable=False,
                server_default='0',
            ),
            sa.ForeignKeyConstraint(
                ['consumed_identity_id'],
                ['miniprogram_identities.id'],
                ondelete='SET NULL',
            ),
            sa.ForeignKeyConstraint(
                ['user_id'],
                ['users.id'],
                ondelete='CASCADE',
            ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('code_hash'),
        )
    else:
        challenge_columns = _columns(
            inspect(bind),
            'miniprogram_link_challenges',
        )
        missing_challenge_columns = (
            CHALLENGE_REQUIRED_COLUMNS - set(challenge_columns)
        )
        if missing_challenge_columns == {'auth_version_at_create'}:
            op.add_column(
                'miniprogram_link_challenges',
                sa.Column(
                    'auth_version_at_create',
                    sa.Integer(),
                    nullable=False,
                    server_default='1',
                ),
            )
        elif missing_challenge_columns:
            raise RuntimeError(
                'cross platform identity migration aborted: '
                'invalid_challenge_columns='
                f'{sorted(missing_challenge_columns)}'
            )

    inspector = inspect(bind)
    challenge_indexes = {
        item['name']: item
        for item in inspector.get_indexes('miniprogram_link_challenges')
    }
    ordinary_indexes = {
        'ix_mp_link_challenges_user_id': ['user_id'],
        'ix_mp_link_challenges_code_hash': ['code_hash'],
        'ix_mp_link_challenges_expires_at': ['expires_at'],
    }
    for name, columns in ordinary_indexes.items():
        existing = challenge_indexes.get(name)
        if existing is None:
            op.create_index(
                name,
                'miniprogram_link_challenges',
                columns,
                unique=False,
            )
        elif existing.get('unique') or list(
            existing.get('column_names') or []
        ) != columns:
            raise RuntimeError(
                'cross platform identity migration aborted: '
                f'invalid_index={name}'
            )

    active_index = challenge_indexes.get('uq_mp_link_active_user')
    if active_index is not None and not _index_predicate_matches(
        active_index,
        expected_where=ACTIVE_CHALLENGE_INDEX_PREDICATE,
        dialect_name=bind.dialect.name,
    ):
        # 早期候选版可能留下同名整列唯一索引；确认无有效挑战冲突后原地修复。
        op.drop_index(
            'uq_mp_link_active_user',
            table_name='miniprogram_link_challenges',
        )
        active_index = None
    if active_index is None:
        op.create_index(
            'uq_mp_link_active_user',
            'miniprogram_link_challenges',
            ['user_id'],
            unique=True,
            sqlite_where=sa.text(ACTIVE_CHALLENGE_INDEX_PREDICATE),
            postgresql_where=sa.text(ACTIVE_CHALLENGE_INDEX_PREDICATE),
        )
    elif not active_index.get('unique') or list(
        active_index.get('column_names') or []
    ) != ['user_id']:
        raise RuntimeError(
            'cross platform identity migration aborted: '
            'invalid_index=uq_mp_link_active_user'
        )


def _preflight_downgrade(
    bind=None,
    inspector=None,
    *,
    include_lower_chain=True,
):
    """任何跨端身份数据存在时拒绝破坏性回退。"""
    bind = bind or op.get_bind()
    _require_partial_index_dialect(bind)
    inspector = inspector or inspect(bind)
    if include_lower_chain:
        previous_migration = importlib.import_module(
            'migrations.versions.0026_cooling_coordinate_verification'
        )
        previous_migration._preflight_downgrade(bind, inspector)

    tables = set(inspector.get_table_names())
    if 'miniprogram_link_challenges' in tables:
        count = bind.execute(sa.text(
            'SELECT COUNT(*) FROM miniprogram_link_challenges'
        )).scalar_one()
        if count:
            raise RuntimeError(
                'cross platform identity downgrade aborted: '
                f'link_challenge_count={count}'
            )
    if 'users' in tables:
        columns = _columns(inspector, 'users')
        _validate_columns(columns, USER_COLUMNS, 'user')
        _validate_account_origins(bind, columns)
        predicates = [
            f'{name} IS NOT NULL'
            for name in ('phone_normalized', 'phone_verified_at')
            if name in columns
        ]
        if predicates:
            count = bind.execute(sa.text(
                f'''SELECT COUNT(*) FROM users
                    WHERE {' OR '.join(predicates)}'''
            )).scalar_one()
            if count:
                raise RuntimeError(
                    'cross platform identity downgrade aborted: '
                    f'phone_identity_count={count}'
                )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    _preflight_downgrade(
        bind,
        inspector,
        include_lower_chain=not _targets_only_previous_revision(),
    )
    tables = set(inspector.get_table_names())
    if 'miniprogram_link_challenges' in tables:
        indexes = {
            item['name']
            for item in inspector.get_indexes('miniprogram_link_challenges')
        }
        for name in (
            'uq_mp_link_active_user',
            'ix_mp_link_challenges_expires_at',
            'ix_mp_link_challenges_code_hash',
            'ix_mp_link_challenges_user_id',
        ):
            if name in indexes:
                op.drop_index(name, table_name='miniprogram_link_challenges')
        op.drop_table('miniprogram_link_challenges')

    inspector = inspect(bind)
    if 'miniprogram_identities' in inspector.get_table_names():
        identity_columns = _columns(inspector, 'miniprogram_identities')
        _validate_columns(identity_columns, IDENTITY_COLUMNS, 'identity')
        identity_indexes = {
            item['name']
            for item in inspector.get_indexes('miniprogram_identities')
        }
        if 'uq_miniprogram_identities_user_id' in identity_indexes:
            op.drop_index(
                'uq_miniprogram_identities_user_id',
                table_name='miniprogram_identities',
            )
        for name in (
            'link_locked_until',
            'link_first_failed_at',
            'link_failed_count',
            'binding_auth_version',
        ):
            if name in identity_columns:
                op.drop_column('miniprogram_identities', name)

    inspector = inspect(bind)
    if 'users' not in inspector.get_table_names():
        return
    user_columns = _columns(inspector, 'users')
    _validate_columns(user_columns, USER_COLUMNS, 'user')
    user_indexes = {
        item['name']
        for item in inspector.get_indexes('users')
    }
    for name in (
        'uq_users_verified_phone_normalized',
        'ix_users_phone_normalized',
        'uq_users_phone_normalized',
    ):
        if name in user_indexes:
            op.drop_index(name, table_name='users')
    if 'phone_verified_at' in user_columns:
        op.drop_column('users', 'phone_verified_at')
    if 'phone_normalized' in user_columns:
        op.drop_column('users', 'phone_normalized')
    if 'account_origin' in user_columns:
        op.drop_column('users', 'account_origin')
