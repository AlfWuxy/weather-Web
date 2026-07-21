# -*- coding: utf-8 -*-
"""认证版本迁移与 head 降级保护回归。"""

from datetime import date
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
HEAD_REVISION = '0026_cooling_coordinate_verify'
PREVIOUS_REVISION = '0022_private_health_indexes'


def _create_app(monkeypatch, database_path):
    """创建完全隔离的迁移测试应用。"""
    monkeypatch.setenv('DATABASE_URI', f'sqlite:///{database_path.as_posix()}')
    monkeypatch.setenv('SECRET_KEY', 'auth-version-migration-test-secret')
    monkeypatch.setenv('PAIR_TOKEN_PEPPER', 'auth-version-migration-pair-pepper')
    monkeypatch.setenv('DEBUG', 'true')
    monkeypatch.setenv('DEMO_MODE', '1')
    monkeypatch.setenv('RATE_LIMIT_STORAGE_URI', 'memory://')
    monkeypatch.setenv('REDIS_URL', '')
    monkeypatch.setenv('QWEATHER_KEY', '')
    monkeypatch.setenv('QWEATHER_API_BASE', '')
    monkeypatch.setenv('AMAP_KEY', '')
    monkeypatch.setenv('SILICONFLOW_API_KEY', '')
    monkeypatch.setenv('SENTRY_DSN', '')
    monkeypatch.delenv('DEFAULT_ADMIN_USERNAME', raising=False)
    monkeypatch.delenv('DEFAULT_ADMIN_PASSWORD', raising=False)

    from core.app import create_app

    return create_app()


def _alembic_config(app):
    config = Config(str(ROOT_DIR / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', app.config['SQLALCHEMY_DATABASE_URI'])
    config.set_main_option('script_location', str(ROOT_DIR / 'migrations'))
    return config


def _initialize(monkeypatch, database_path):
    app = _create_app(monkeypatch, database_path)
    result = app.test_cli_runner().invoke(args=['init-db'])
    assert result.exit_code == 0, result.output
    return app, _alembic_config(app)


def _dispose(app):
    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def _revision_and_columns(database_path):
    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]
        columns = {
            table_name: {
                row[1]: row
                for row in connection.execute(f'PRAGMA table_info({table_name})')
            }
            for table_name in (
                'users',
                'daily_status',
                'weather_alerts',
                'debriefs',
            )
        }
    return revision, columns


def test_auth_version_migration_round_trip_preserves_clean_users(monkeypatch, tmp_path):
    """全为版本 1 时允许降级，并能无损恢复到 head。"""
    database_path = tmp_path / 'auth-version-round-trip.db'
    app, config = _initialize(monkeypatch, database_path)

    from core.db_models import User
    from core.extensions import db

    with app.app_context():
        user = User(username='auth-version-round-trip-user')
        user.set_password('MigrationPassword1!')
        db.session.add(user)
        db.session.commit()
        user_id = int(user.id)
    _dispose(app)

    command.downgrade(config, PREVIOUS_REVISION)
    revision, columns = _revision_and_columns(database_path)
    assert revision == PREVIOUS_REVISION
    assert 'auth_version' not in columns['users']

    command.upgrade(config, 'head')
    revision, columns = _revision_and_columns(database_path)
    assert revision == HEAD_REVISION
    assert columns['users']['auth_version'][3] == 1
    assert columns['users']['auth_version'][2].upper() == 'INTEGER'
    with sqlite3.connect(database_path) as connection:
        restored = connection.execute(
            'SELECT auth_version FROM users WHERE id = ?',
            (user_id,),
        ).fetchone()[0]
    assert restored == 1


def test_wxpusher_consent_migration_resets_history_and_downgrades_fail_closed(
    monkeypatch,
    tmp_path,
):
    """迁移不信任旧开关，重复升级安全，降级前也先关闭推送。"""
    database_path = tmp_path / 'wxpusher-consent-round-trip.db'
    app, config = _initialize(monkeypatch, database_path)

    from core.db_models import User
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        user = User(
            username='wxpusher-consent-migration-user',
            wxpusher_uid='UID_MIGRATION',
            push_enabled=True,
            wxpusher_consent_version=app.config['WX_MINIPROGRAM_PRIVACY_VERSION'],
            wxpusher_consented_at=utcnow(),
        )
        user.set_password('MigrationPassword1!')
        db.session.add(user)
        db.session.commit()
        user_id = int(user.id)
    _dispose(app)

    command.downgrade(config, '0023_auth_session_version')
    revision, columns = _revision_and_columns(database_path)
    assert revision == '0023_auth_session_version'
    assert 'wxpusher_consent_version' not in columns['users']
    assert 'wxpusher_consented_at' not in columns['users']
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            'SELECT push_enabled FROM users WHERE id = ?',
            (user_id,),
        ).fetchone()[0] == 0
        # 模拟迁移重试前已存在正确同名列的部分状态。
        connection.execute(
            'ALTER TABLE users ADD COLUMN wxpusher_consent_version VARCHAR(64)'
        )
        connection.execute(
            'ALTER TABLE users ADD COLUMN wxpusher_consented_at DATETIME'
        )
        connection.execute(
            '''UPDATE users
               SET push_enabled = 1,
                   wxpusher_consent_version = 'privacy-legacy',
                   wxpusher_consented_at = '2026-07-18 08:00:00'
               WHERE id = ?''',
            (user_id,),
        )
        connection.commit()

    command.upgrade(config, 'head')
    command.upgrade(config, 'head')
    revision, columns = _revision_and_columns(database_path)
    assert revision == HEAD_REVISION
    assert columns['users']['wxpusher_consent_version'][2].upper() == 'VARCHAR(64)'
    assert columns['users']['wxpusher_consent_version'][3] == 0
    assert columns['users']['wxpusher_consented_at'][2].upper() == 'DATETIME'
    assert columns['users']['wxpusher_consented_at'][3] == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            '''SELECT push_enabled,
                      wxpusher_consent_version,
                      wxpusher_consented_at
               FROM users WHERE id = ?''',
            (user_id,),
        ).fetchone() == (0, None, None)
        connection.execute(
            '''UPDATE users
               SET push_enabled = 1,
                   wxpusher_consent_version = 'privacy-current',
                   wxpusher_consented_at = '2026-07-18 09:00:00'
               WHERE id = ?''',
            (user_id,),
        )
        connection.commit()

    command.downgrade(config, '0023_auth_session_version')
    revision, columns = _revision_and_columns(database_path)
    assert revision == '0023_auth_session_version'
    assert 'wxpusher_consent_version' not in columns['users']
    assert 'wxpusher_consented_at' not in columns['users']
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            'SELECT push_enabled FROM users WHERE id = ?',
            (user_id,),
        ).fetchone()[0] == 0


def test_health_sensitive_consent_migration_starts_null_and_blocks_lossy_downgrade(
    monkeypatch,
    tmp_path,
):
    """旧数据不继承一般隐私同意，真实健康回执存在时降级必须失败关闭。"""
    database_path = tmp_path / 'health-sensitive-consent-migration.db'
    app, config = _initialize(monkeypatch, database_path)

    from core.db_models import User
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        user = User(username='health-sensitive-consent-migration-user')
        user.set_password('MigrationPassword1!')
        db.session.add(user)
        db.session.commit()
        user_id = int(user.id)
    _dispose(app)

    revision, columns = _revision_and_columns(database_path)
    assert revision == HEAD_REVISION
    assert columns['users']['health_sensitive_consent_version'][2].upper() == (
        'VARCHAR(64)'
    )
    assert columns['users']['health_sensitive_consent_version'][3] == 0
    assert columns['users']['health_sensitive_consented_at'][2].upper() == 'DATETIME'
    assert columns['users']['health_sensitive_consented_at'][3] == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            '''SELECT health_sensitive_consent_version,
                      health_sensitive_consented_at
               FROM users WHERE id = ?''',
            (user_id,),
        ).fetchone() == (None, None)

    # 模拟仅新增版本列后中断：重试要补齐时间列，并清空不可信旧值。
    command.downgrade(config, '0024_wxpusher_consent_receipt')
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            'ALTER TABLE users ADD COLUMN health_sensitive_consent_version VARCHAR(64)'
        )
        connection.execute(
            '''UPDATE users
               SET health_sensitive_consent_version = 'privacy-legacy'
               WHERE id = ?''',
            (user_id,),
        )
        connection.commit()
    command.upgrade(config, 'head')
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            '''SELECT health_sensitive_consent_version,
                      health_sensitive_consented_at
               FROM users WHERE id = ?''',
            (user_id,),
        ).fetchone() == (None, None)

    app = _create_app(monkeypatch, database_path)
    with app.app_context():
        restored = db.session.get(User, user_id)
        restored.health_sensitive_consent_version = app.config[
            'WX_MINIPROGRAM_PRIVACY_VERSION'
        ]
        restored.health_sensitive_consented_at = utcnow()
        db.session.commit()
    _dispose(app)

    with pytest.raises(RuntimeError, match='nonempty_receipt_count=1'):
        command.downgrade(config, '0024_wxpusher_consent_receipt')
    revision, columns = _revision_and_columns(database_path)
    assert revision == HEAD_REVISION
    assert 'health_sensitive_consent_version' in columns['users']
    assert 'health_sensitive_consented_at' in columns['users']


def test_cooling_migration_single_step_downgrade_preserves_health_consent(
    monkeypatch,
    tmp_path,
):
    """只回退 0026 时应保留 0025 健康回执及其数据。"""
    database_path = tmp_path / 'cooling-single-step-downgrade.db'
    app, config = _initialize(monkeypatch, database_path)

    from core.db_models import User
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        user = User(username='cooling-single-step-downgrade-user')
        user.set_password('MigrationPassword1!')
        user.health_sensitive_consent_version = app.config[
            'WX_MINIPROGRAM_PRIVACY_VERSION'
        ]
        user.health_sensitive_consented_at = utcnow()
        db.session.add(user)
        db.session.commit()
        user_id = int(user.id)
    _dispose(app)

    command.downgrade(config, '0025_health_sensitive_consent')

    revision, columns = _revision_and_columns(database_path)
    with sqlite3.connect(database_path) as connection:
        cooling_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(cooling_resources)')
        }
    assert revision == '0025_health_sensitive_consent'
    assert 'coordinate_system' not in cooling_columns
    assert 'coordinate_source' not in cooling_columns
    assert 'coordinate_verified_at' not in cooling_columns
    assert 'health_sensitive_consent_version' in columns['users']
    assert 'health_sensitive_consented_at' in columns['users']
    with sqlite3.connect(database_path) as connection:
        version, consented_at = connection.execute(
            '''SELECT health_sensitive_consent_version,
                      health_sensitive_consented_at
               FROM users WHERE id = ?''',
            (user_id,),
        ).fetchone()
    assert version == '2026-07-21'
    assert consented_at

    command.upgrade(config, 'head')
    command.downgrade(config, '-1')
    relative_revision, relative_columns = _revision_and_columns(database_path)
    assert relative_revision == '0025_health_sensitive_consent'
    assert 'health_sensitive_consent_version' in relative_columns['users']
    assert 'health_sensitive_consented_at' in relative_columns['users']


@pytest.mark.parametrize(
    ('column_sql', 'invalid_column'),
    (
        ('health_sensitive_consent_version INTEGER', 'health_sensitive_consent_version'),
        ('health_sensitive_consented_at TEXT', 'health_sensitive_consented_at'),
    ),
)
def test_health_sensitive_consent_migration_rejects_invalid_existing_columns(
    monkeypatch,
    tmp_path,
    column_sql,
    invalid_column,
):
    """错误类型的同名列必须在任何新增列之前失败关闭。"""
    database_path = tmp_path / f'health-consent-invalid-{invalid_column}.db'
    app, config = _initialize(monkeypatch, database_path)
    _dispose(app)
    command.downgrade(config, '0024_wxpusher_consent_receipt')

    with sqlite3.connect(database_path) as connection:
        connection.execute(f'ALTER TABLE users ADD COLUMN {column_sql}')
        connection.commit()

    with pytest.raises(RuntimeError, match=invalid_column):
        command.upgrade(config, 'head')

    revision, columns = _revision_and_columns(database_path)
    assert revision == '0024_wxpusher_consent_receipt'
    assert invalid_column in columns['users']
    other_column = (
        'health_sensitive_consented_at'
        if invalid_column == 'health_sensitive_consent_version'
        else 'health_sensitive_consent_version'
    )
    assert other_column not in columns['users']


def test_wxpusher_relative_one_step_downgrade_stops_at_auth_revision(
    monkeypatch,
    tmp_path,
):
    """相对降级一阶只移除回执列，不误触发 0023 丢数据保护。"""
    database_path = tmp_path / 'wxpusher-relative-one-step.db'
    app, config = _initialize(monkeypatch, database_path)

    from core.db_models import User
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        user = User(
            username='wxpusher-relative-one-step-user',
            auth_version=2,
            wxpusher_uid='UID_RELATIVE_ONE',
            push_enabled=True,
            wxpusher_consent_version=app.config['WX_MINIPROGRAM_PRIVACY_VERSION'],
            wxpusher_consented_at=utcnow(),
        )
        user.set_password('MigrationPassword1!')
        db.session.add(user)
        db.session.commit()
        user_id = int(user.id)
    _dispose(app)

    # 先越过新增的 0026 与 0025，再单独验证 0024 的相对一阶语义。
    command.downgrade(config, '0024_wxpusher_consent_receipt')
    command.downgrade(config, '-1')

    revision, columns = _revision_and_columns(database_path)
    assert revision == '0023_auth_session_version'
    assert 'auth_version' in columns['users']
    assert 'wxpusher_consent_version' not in columns['users']
    assert 'wxpusher_consented_at' not in columns['users']
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            'SELECT auth_version, push_enabled FROM users WHERE id = ?',
            (user_id,),
        ).fetchone() == (2, 0)


def test_wxpusher_relative_four_step_downgrade_runs_auth_guard(
    monkeypatch,
    tmp_path,
):
    """相对降级四阶会跨越 0023，并在首个 DDL 前阻断。"""
    database_path = tmp_path / 'wxpusher-relative-two-step.db'
    app, config = _initialize(monkeypatch, database_path)

    from core.db_models import User
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        user = User(
            username='wxpusher-relative-two-step-user',
            auth_version=2,
            wxpusher_uid='UID_RELATIVE_TWO',
            push_enabled=True,
            wxpusher_consent_version=app.config['WX_MINIPROGRAM_PRIVACY_VERSION'],
            wxpusher_consented_at=utcnow(),
        )
        user.set_password('MigrationPassword1!')
        db.session.add(user)
        db.session.commit()
        user_id = int(user.id)
    _dispose(app)

    with pytest.raises(RuntimeError, match='auth_version_count=1'):
        command.downgrade(config, '-4')

    revision, columns = _revision_and_columns(database_path)
    assert revision == HEAD_REVISION
    assert 'wxpusher_consent_version' in columns['users']
    assert 'wxpusher_consented_at' in columns['users']
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            '''SELECT auth_version,
                      push_enabled,
                      wxpusher_consent_version
               FROM users WHERE id = ?''',
            (user_id,),
        ).fetchone() == (2, 1, app.config['WX_MINIPROGRAM_PRIVACY_VERSION'])


@pytest.mark.parametrize(
    ('guard_case', 'expected_error'),
    (
        ('auth_version', 'auth_version_count=1'),
        ('elder_actions', 'elder_action_count=1'),
        ('dedupe_key', 'dedupe_key_count=1'),
        ('debrief', 'unrepresentable_debrief_count=1'),
    ),
)
def test_head_downgrade_guard_preserves_all_new_columns_and_data(
    monkeypatch,
    tmp_path,
    guard_case,
    expected_error,
):
    """任何新语义存在时，0023 在首个 DDL 前阻断整条降级链。"""
    database_path = tmp_path / f'auth-version-guard-{guard_case}.db'
    app, config = _initialize(monkeypatch, database_path)

    from core.db_models import DailyStatus, Debrief, Pair, User, WeatherAlert
    from core.extensions import db
    from core.security import hash_short_code
    from core.time_utils import utcnow

    with app.app_context():
        owner = User(username=f'auth-version-guard-{guard_case}')
        owner.set_password('MigrationPassword1!')
        db.session.add(owner)
        db.session.flush()
        owner_id = int(owner.id)
        if guard_case == 'auth_version':
            owner.auth_version = 2
        elif guard_case == 'dedupe_key':
            db.session.add(WeatherAlert(
                location='都昌县',
                alert_type='高温',
                alert_level='橙色',
                description='迁移降级保护',
                dedupe_key='a' * 64,
            ))
        else:
            pair = Pair(
                caregiver_id=owner.id,
                community_code='都昌县',
                location_query='都昌县',
                elder_code=f'guard-elder-{guard_case}',
                short_code='25252525' if guard_case == 'elder_actions' else '26262626',
                short_code_hash=hash_short_code(
                    '25252525' if guard_case == 'elder_actions' else '26262626'
                ),
                status='active',
                created_at=utcnow(),
                last_active_at=utcnow(),
            )
            db.session.add(pair)
            db.session.flush()
            if guard_case == 'elder_actions':
                db.session.add(DailyStatus(
                    pair_id=pair.id,
                    status_date=date(2026, 7, 18),
                    community_code='都昌县',
                    elder_actions='["补水"]',
                ))
            else:
                db.session.add(Debrief(
                    date=date(2026, 7, 18),
                    community_code='都昌县',
                    owner_user_id=owner.id,
                    origin_pair_id=pair.id,
                    pair_id=None,
                    question_1='无法由旧结构表达',
                    created_at=utcnow(),
                ))
        db.session.commit()
    _dispose(app)

    with pytest.raises(RuntimeError, match=expected_error):
        command.downgrade(config, PREVIOUS_REVISION)

    revision, columns = _revision_and_columns(database_path)
    assert revision == HEAD_REVISION
    assert 'auth_version' in columns['users']
    assert 'elder_actions' in columns['daily_status']
    assert 'dedupe_key' in columns['weather_alerts']
    assert {'owner_user_id', 'origin_pair_id'} <= set(columns['debriefs'])
    with sqlite3.connect(database_path) as connection:
        if guard_case == 'auth_version':
            kept = connection.execute(
                'SELECT auth_version FROM users WHERE id = ?',
                (owner_id,),
            ).fetchone()[0]
            assert kept == 2
        elif guard_case == 'elder_actions':
            assert connection.execute(
                'SELECT elder_actions FROM daily_status'
            ).fetchone()[0] == '["补水"]'
        elif guard_case == 'dedupe_key':
            assert connection.execute(
                'SELECT dedupe_key FROM weather_alerts'
            ).fetchone()[0] == 'a' * 64
        else:
            assert connection.execute(
                'SELECT origin_pair_id, pair_id, question_1 FROM debriefs'
            ).fetchone() == (1, None, '无法由旧结构表达')


@pytest.mark.parametrize(
    ('column_ddl', 'expected_error', 'expected_type', 'expected_not_null'),
    (
        (
            'auth_version INTEGER NULL',
            'auth_version_invalid_schema',
            'INTEGER',
            0,
        ),
        (
            "auth_version TEXT NOT NULL DEFAULT '1'",
            'auth_version_invalid_schema',
            'TEXT',
            1,
        ),
        (
            'auth_version INTEGER NOT NULL DEFAULT 0',
            'auth_version_invalid_value_count=1',
            'INTEGER',
            1,
        ),
    ),
)
def test_upgrade_rejects_untrusted_existing_auth_version_column(
    monkeypatch,
    tmp_path,
    column_ddl,
    expected_error,
    expected_type,
    expected_not_null,
):
    """错误的同名列不能让 Alembic 误盖 head。"""
    database_path = tmp_path / f'auth-version-invalid-{expected_type}-{expected_not_null}.db'
    app, config = _initialize(monkeypatch, database_path)

    from core.db_models import User
    from core.extensions import db

    with app.app_context():
        user = User(username=f'invalid-auth-column-{expected_type}-{expected_not_null}')
        user.set_password('MigrationPassword1!')
        db.session.add(user)
        db.session.commit()
    _dispose(app)
    command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute(f'ALTER TABLE users ADD COLUMN {column_ddl}')
        connection.commit()

    with pytest.raises(RuntimeError, match=expected_error):
        command.upgrade(config, 'head')

    revision, columns = _revision_and_columns(database_path)
    assert revision == PREVIOUS_REVISION
    column = columns['users']['auth_version']
    assert column[2].upper() == expected_type
    assert column[3] == expected_not_null
    with sqlite3.connect(database_path) as connection:
        kept_value = connection.execute(
            'SELECT auth_version FROM users'
        ).fetchone()[0]
    if expected_type == 'TEXT':
        assert kept_value == '1'
    elif expected_not_null:
        assert kept_value == 0
    else:
        assert kept_value is None
