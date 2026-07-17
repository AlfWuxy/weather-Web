# -*- coding: utf-8 -*-
"""空数据库初始化链路回归测试。"""
from pathlib import Path
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect


ROOT_DIR = Path(__file__).resolve().parents[1]


def _alembic_config(app):
    """构造指向当前临时数据库的 Alembic 配置。"""
    config = Config(str(ROOT_DIR / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', app.config['SQLALCHEMY_DATABASE_URI'])
    config.set_main_option('script_location', str(ROOT_DIR / 'migrations'))
    return config


def _create_test_app(monkeypatch, database_path):
    """创建只使用临时 SQLite 的应用实例。"""
    monkeypatch.setenv('DATABASE_URI', f'sqlite:///{database_path.as_posix()}')
    monkeypatch.setenv('SECRET_KEY', 'database-bootstrap-test-secret-key')
    monkeypatch.setenv('PAIR_TOKEN_PEPPER', 'database-bootstrap-test-pair-pepper')
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


def _assert_schema_is_at_head(app):
    """确认模型表已经创建，且 Alembic 版本位于当前 head。"""
    from core.extensions import db

    alembic_config = _alembic_config(app)
    expected_head = ScriptDirectory.from_config(alembic_config).get_current_head()

    with app.app_context():
        actual_tables = set(inspect(db.engine).get_table_names())
        expected_tables = set(db.metadata.tables)
        assert expected_tables <= actual_tables

        with db.engine.connect() as connection:
            current_revision = MigrationContext.configure(connection).get_current_revision()
        assert current_revision == expected_head


def test_init_db_bootstraps_fresh_database_and_is_idempotent(monkeypatch, tmp_path):
    """全新数据库应能初始化，并允许安全重复执行。"""
    app = _create_test_app(monkeypatch, tmp_path / 'fresh.db')
    runner = app.test_cli_runner()

    first_result = runner.invoke(args=['init-db'])
    assert first_result.exit_code == 0, first_result.output
    assert 'Database initialized.' in first_result.output
    _assert_schema_is_at_head(app)
    assert app.test_client().get('/register').status_code == 200

    from core.db_models import User
    from core.extensions import db

    # 第二次初始化必须走已有数据库升级路径，并保留原有业务数据。
    with app.app_context():
        user = User(username='bootstrap-existing-user')
        user.set_password('bootstrap-test-password')
        db.session.add(user)
        db.session.commit()

    second_result = runner.invoke(args=['init-db'])
    assert second_result.exit_code == 0, second_result.output
    _assert_schema_is_at_head(app)
    with app.app_context():
        assert User.query.filter_by(username='bootstrap-existing-user').count() == 1


def test_init_db_recovers_empty_alembic_version_shell(monkeypatch, tmp_path):
    """此前失败只留下版本表时，初始化应能重新完成。"""
    app = _create_test_app(monkeypatch, tmp_path / 'retry.db')

    from core.extensions import db

    with app.app_context():
        with db.engine.begin() as connection:
            connection.exec_driver_sql(
                'CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)'
            )

    result = app.test_cli_runner().invoke(args=['init-db'])
    assert result.exit_code == 0, result.output
    _assert_schema_is_at_head(app)


def test_miniprogram_migration_round_trip_removes_member_id(monkeypatch, tmp_path):
    """0011 降级必须清理 member_id，再升级后恢复完整 owner 约束。"""
    app = _create_test_app(monkeypatch, tmp_path / 'miniprogram-round-trip.db')
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    command.downgrade(alembic_config, '0010_action_token_hardening')

    with app.app_context():
        downgraded = inspect(db.engine)
        assessment_columns = {
            column['name']
            for column in downgraded.get_columns('health_risk_assessments')
        }
        assert 'member_id' not in assessment_columns
        assert 'miniprogram_sessions' not in downgraded.get_table_names()
        db.session.remove()
        db.engine.dispose()

    command.upgrade(alembic_config, 'head')

    with app.app_context():
        upgraded = inspect(db.engine)
        assessment_columns = {
            column['name']
            for column in upgraded.get_columns('health_risk_assessments')
        }
        assert 'member_id' in assessment_columns
        owner_fk = next(
            foreign_key
            for foreign_key in upgraded.get_foreign_keys('miniprogram_sessions')
            if foreign_key.get('constrained_columns') == ['identity_id', 'user_id']
        )
        assert owner_fk['referred_columns'] == ['id', 'user_id']
        assert owner_fk.get('options', {}).get('ondelete') == 'CASCADE'


def test_miniprogram_acquisition_migration_backfills_source_and_index(
    monkeypatch,
    tmp_path,
):
    """0015 只从小程序首条登录事件恢复来源，并建立 cohort 索引。"""
    database_path = tmp_path / 'miniprogram-acquisition.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.db_models import MiniProgramIdentity, User
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        family_user = User(username='migration-family-source')
        web_only_user = User(username='migration-web-only-source')
        family_user.set_password('migration-test-password')
        web_only_user.set_password('migration-test-password')
        db.session.add_all([family_user, web_only_user])
        db.session.flush()
        family_user_id = family_user.id
        web_only_user_id = web_only_user.id
        now = utcnow()
        db.session.add_all([
            MiniProgramIdentity(
                user_id=family_user_id,
                openid_hash='migration-family-openid',
                privacy_consent_version='privacy-v1',
                privacy_consented_at=now,
                acquisition_source='direct',
                created_at=now,
            ),
            MiniProgramIdentity(
                user_id=web_only_user_id,
                openid_hash='migration-web-only-openid',
                privacy_consent_version='privacy-v1',
                privacy_consented_at=now,
                acquisition_source='direct',
                created_at=now,
            ),
        ])
        db.session.commit()
        db.session.remove()
        db.engine.dispose()

    command.downgrade(alembic_config, '0014_wxpusher_reconsent')

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(miniprogram_identities)')
        }
        assert 'acquisition_source' not in columns
        connection.execute(
            '''
            INSERT INTO usage_events (
                user_id, event_type, meta_json, source, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ''',
            (
                family_user_id,
                'wechat_login_success',
                '{"from":"family_share"}',
                'miniprogram',
                '2026-07-17 01:00:00',
            ),
        )
        connection.execute(
            '''
            INSERT INTO usage_events (
                user_id, event_type, meta_json, source, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ''',
            (
                web_only_user_id,
                'wechat_login_success',
                '{"from":"family_share"}',
                'web',
                '2026-07-17 01:00:00',
            ),
        )
        connection.commit()

    command.upgrade(alembic_config, 'head')

    with sqlite3.connect(database_path) as connection:
        sources = dict(
            connection.execute(
                '''
                SELECT user_id, acquisition_source
                FROM miniprogram_identities
                ORDER BY user_id
                '''
            )
        )
        indexes = {
            row[1]
            for row in connection.execute('PRAGMA index_list(miniprogram_identities)')
        }
    assert sources[family_user_id] == 'family_share'
    assert sources[web_only_user_id] == 'unknown'
    assert 'ix_miniprogram_identities_created_at' in indexes


def test_acquisition_upgrade_from_0011_excludes_erased_legacy_source(
    monkeypatch,
    tmp_path,
):
    """0012 已清除来源证据时，0015 必须标 unknown，禁止误归 direct。"""
    database_path = tmp_path / 'miniprogram-acquisition-legacy.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.db_models import MiniProgramIdentity, User
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        user = User(username='migration-legacy-source')
        user.set_password('migration-test-password')
        db.session.add(user)
        db.session.flush()
        user_id = user.id
        db.session.add(
            MiniProgramIdentity(
                user_id=user_id,
                openid_hash='migration-legacy-source-openid',
                privacy_consent_version='privacy-v1',
                privacy_consented_at=utcnow(),
                acquisition_source='direct',
                created_at=utcnow(),
            )
        )
        db.session.commit()
        db.session.remove()
        db.engine.dispose()

    command.downgrade(alembic_config, '0011_miniprogram_runtime')
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''
            INSERT INTO usage_events (
                user_id, event_type, meta_json, source, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ''',
            (
                user_id,
                'wechat_login_success',
                '{"from":"family_share"}',
                'miniprogram',
                '2026-07-17 01:00:00',
            ),
        )
        connection.commit()

    command.upgrade(alembic_config, 'head')

    with sqlite3.connect(database_path) as connection:
        source = connection.execute(
            'SELECT acquisition_source FROM miniprogram_identities WHERE user_id = ?',
            (user_id,),
        ).fetchone()[0]
        retained_meta = connection.execute(
            'SELECT meta_json FROM usage_events WHERE user_id = ?',
            (user_id,),
        ).fetchone()[0]
    assert retained_meta is None
    assert source == 'unknown'


def test_usage_event_foreign_key_repair_anonymizes_orphans(monkeypatch, tmp_path):
    """历史孤儿埋点应保留匿名计数，并持续允许父级安全删除。"""
    database_path = tmp_path / 'usage-event-repair.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.db_models import UsageEvent, User
    from core.extensions import db

    with app.app_context():
        valid_user = User(username='usage-event-valid-user')
        valid_user.set_password('usage-event-valid-password')
        db.session.add(valid_user)
        db.session.flush()
        valid_user_id = valid_user.id
        db.session.add(
            UsageEvent(
                user_id=valid_user_id,
                event_type='valid_event',
                meta_json='{"kept":true}',
                source='web',
            )
        )
        db.session.commit()

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    command.downgrade(alembic_config, '0011_miniprogram_runtime')

    # 复现旧版本关闭 SQLite 外键约束时分别遗留的三类孤儿引用。
    with sqlite3.connect(database_path) as connection:
        connection.execute('PRAGMA foreign_keys = OFF')
        for column_name, missing_id, event_type in (
            ('user_id', 99991, 'orphan_user_event'),
            ('pair_id', 99992, 'orphan_pair_event'),
            ('member_id', 99993, 'orphan_member_event'),
        ):
            connection.execute(
                f'''
                INSERT INTO usage_events (
                    {column_name},
                    event_type,
                    meta_json,
                    source
                ) VALUES (?, ?, ?, ?)
                ''',
                (
                    missing_id,
                    event_type,
                    '{"location_query":"private-location"}',
                    'web',
                ),
            )
        connection.commit()

    command.upgrade(alembic_config, 'head')

    with app.app_context():
        repaired_rows = db.session.execute(
            db.text(
                '''
                SELECT user_id, pair_id, member_id, event_type, meta_json
                FROM usage_events
                WHERE event_type LIKE 'orphan_%'
                ORDER BY event_type
                '''
            )
        ).all()
        assert len(repaired_rows) == 3
        for row in repaired_rows:
            assert row.user_id is None
            assert row.pair_id is None
            assert row.member_id is None
            assert row.meta_json is None

        valid_row = db.session.execute(
            db.text(
                '''
                SELECT user_id, meta_json
                FROM usage_events
                WHERE event_type = 'valid_event'
                '''
            )
        ).one()
        assert valid_row.user_id == valid_user_id
        assert valid_row.meta_json is None

        usage_event_fks = {
            tuple(foreign_key.get('constrained_columns') or ()): foreign_key
            for foreign_key in inspect(db.engine).get_foreign_keys('usage_events')
        }
        for column_name in ('user_id', 'pair_id', 'member_id'):
            assert usage_event_fks[(column_name,)]['options']['ondelete'] == 'SET NULL'

        violations = db.session.execute(db.text('PRAGMA foreign_key_check')).all()
        assert violations == []

        # 约束开启后，父级删除应自动匿名化引用并保持事件可计数。
        db.session.execute(
            db.text('DELETE FROM users WHERE id = :user_id'),
            {'user_id': valid_user_id},
        )
        db.session.commit()
        deleted_parent_event = db.session.execute(
            db.text(
                '''
                SELECT user_id, event_type, meta_json
                FROM usage_events
                WHERE event_type = 'valid_event'
                '''
            )
        ).one()
        assert deleted_parent_event.user_id is None
        assert deleted_parent_event.event_type == 'valid_event'
        assert deleted_parent_event.meta_json is None


def test_alert_delivery_idempotency_migration_deduplicates_safely(
    monkeypatch,
    tmp_path,
):
    """0016 优先保留 sent，再保留同级最新记录，并固化三元唯一约束。"""
    database_path = tmp_path / 'alert-delivery-idempotency.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.db_models import User, WeatherAlert
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        user = User(username='delivery-migration-user')
        user.set_password('migration-test-password')
        db.session.add(user)
        first_alert = WeatherAlert(
            alert_date=utcnow(),
            location='116.20,29.27',
            alert_type='heat_threshold',
            alert_level='阈值',
        )
        second_alert = WeatherAlert(
            alert_date=utcnow(),
            location='116.20,29.27',
            alert_type='cold_threshold',
            alert_level='阈值',
        )
        db.session.add_all([first_alert, second_alert])
        db.session.flush()
        user_id = int(user.id)
        first_alert_id = int(first_alert.id)
        second_alert_id = int(second_alert.id)
        db.session.commit()
        db.session.remove()
        db.engine.dispose()

    command.downgrade(alembic_config, '0015_miniprogram_acquisition')

    with sqlite3.connect(database_path) as connection:
        rows = [
            # 即使 failed 更晚，也必须保留 sent；多个 sent 中保留时间最新的一条。
            (first_alert_id, user_id, ' WXPUSHER ', 'sent', 'keep-sent-old', '2026-07-18 01:00:00'),
            (first_alert_id, user_id, 'wxpusher', 'failed', 'drop-failed-newer', '2026-07-18 03:00:00'),
            (first_alert_id, user_id, 'wxpusher', 'sent', 'keep-sent-new', '2026-07-18 02:00:00'),
            # 没有 sent 时按时间和 id 保留最新记录；空渠道统一收敛为 wxpusher。
            (second_alert_id, user_id, None, 'uncertain', 'drop-uncertain-old', '2026-07-18 01:00:00'),
            (second_alert_id, user_id, '', 'failed', 'keep-failed-new', '2026-07-18 02:00:00'),
        ]
        connection.executemany(
            '''
            INSERT INTO alert_deliveries (
                alert_id,
                user_id,
                channel,
                status,
                delivery_token,
                sent_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ''',
            rows,
        )
        connection.commit()

    command.upgrade(alembic_config, 'head')

    with sqlite3.connect(database_path) as connection:
        kept = connection.execute(
            '''
            SELECT alert_id, status, delivery_token, channel
            FROM alert_deliveries
            ORDER BY alert_id
            '''
        ).fetchall()
        table_info = {
            row[1]: row
            for row in connection.execute('PRAGMA table_info(alert_deliveries)')
        }
        unique_index_names = [
            row[1]
            for row in connection.execute('PRAGMA index_list(alert_deliveries)')
            if bool(row[2])
        ]
        unique_column_sets = {
            tuple(
                row[2]
                for row in connection.execute(f'PRAGMA index_info("{index_name}")')
            )
            for index_name in unique_index_names
        }
        index_names = {
            row[1]
            for row in connection.execute('PRAGMA index_list(alert_deliveries)')
        }

        assert kept == [
            (first_alert_id, 'sent', 'keep-sent-new', 'wxpusher'),
            (second_alert_id, 'failed', 'keep-failed-new', 'wxpusher'),
        ]
        assert table_info['channel'][3] == 1
        assert table_info['status'][3] == 1
        assert table_info['attempt_count'][3] == 1
        assert {'reviewed_at', 'reviewed_by_user_id', 'review_action'} <= set(table_info)
        assert 'ix_alert_deliveries_status_sent_at' in index_names
        assert ('alert_id', 'user_id', 'channel') in unique_column_sets

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                INSERT INTO alert_deliveries (
                    alert_id,
                    user_id,
                    channel,
                    status,
                    delivery_token
                ) VALUES (?, ?, 'wxpusher', 'sending', 'duplicate-key')
                ''',
                (first_alert_id, user_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                INSERT INTO alert_deliveries (
                    alert_id,
                    user_id,
                    channel,
                    status,
                    delivery_token
                ) VALUES (?, ?, NULL, 'sending', 'null-channel')
                ''',
                (second_alert_id, user_id),
            )
