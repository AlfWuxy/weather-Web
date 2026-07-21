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
    assert app.logger.disabled is False
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
    assert app.logger.disabled is False
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


def test_cooling_coordinate_verification_migration_is_idempotent_and_resets_history(
    monkeypatch,
    tmp_path,
):
    """历史坐标保持未核验，迁移中断重试也应补齐全部来源字段。"""
    database_path = tmp_path / 'cooling-coordinate-verification.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    command.downgrade(alembic_config, '0025_health_sensitive_consent')

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''INSERT INTO cooling_resources (
                   community_code, name, latitude, longitude, is_active
               ) VALUES (?, ?, ?, ?, ?)''',
            ('测试社区', '历史坐标点位', 29.27, 116.20, 1),
        )
        # 模拟迁移只新增第一列后中断，下一次升级必须安全补齐。
        connection.execute(
            'ALTER TABLE cooling_resources ADD COLUMN coordinate_system VARCHAR(16)'
        )
        connection.execute(
            '''UPDATE cooling_resources
               SET coordinate_system = 'GCJ-02'
               WHERE name = '历史坐标点位' '''
        )
        connection.commit()

    command.upgrade(alembic_config, 'head')
    command.upgrade(alembic_config, 'head')

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row
            for row in connection.execute('PRAGMA table_info(cooling_resources)')
        }
        verification = connection.execute(
            '''SELECT coordinate_system, coordinate_source, coordinate_verified_at
               FROM cooling_resources WHERE name = '历史坐标点位' '''
        ).fetchone()
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]

    assert {
        'coordinate_system',
        'coordinate_source',
        'coordinate_verified_at',
    } <= set(columns)
    assert verification == (None, None, None)
    assert revision == '0026_cooling_coordinate_verify'

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''UPDATE cooling_resources
               SET coordinate_system = 'GCJ-02',
                   coordinate_source = '管理员现场核验',
                   coordinate_verified_at = '2026-07-21 10:00:00'
               WHERE name = '历史坐标点位' '''
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='nonempty_verification_count=1'):
        command.downgrade(alembic_config, '0025_health_sensitive_consent')
    with sqlite3.connect(database_path) as connection:
        protected_revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]
        protected_columns = {
            row[1] for row in connection.execute('PRAGMA table_info(cooling_resources)')
        }
    assert protected_revision == '0026_cooling_coordinate_verify'
    assert set(COLUMN for COLUMN in (
        'coordinate_system',
        'coordinate_source',
        'coordinate_verified_at',
    )) <= protected_columns


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


def test_debrief_owner_migration_backfills_owner_and_origin_without_data_loss(
    monkeypatch,
    tmp_path,
):
    """0018 只用旧 pair 回填 owner 与稳定来源，并固化外键和索引。"""
    database_path = tmp_path / 'debrief-owner-backfill.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.db_models import Pair, User
    from core.extensions import db
    from core.security import hash_short_code
    from core.time_utils import utcnow

    with app.app_context():
        owner = User(username='debrief-migration-owner')
        owner.set_password('migration-test-password')
        db.session.add(owner)
        db.session.flush()
        pair = Pair(
            caregiver_id=owner.id,
            community_code='都昌县',
            location_query='都昌县',
            elder_code='debrief-migration-elder',
            short_code='81818181',
            short_code_hash=hash_short_code('81818181'),
            status='active',
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.commit()
        owner_id = int(owner.id)
        pair_id = int(pair.id)
        db.session.remove()
        db.engine.dispose()

    command.downgrade(alembic_config, '0017_delivery_review_workflow')
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''
            INSERT INTO debriefs (
                date,
                community_code,
                pair_id,
                question_1,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            ''',
            (
                '2026-07-18',
                '都昌县',
                pair_id,
                '迁移前已有复盘',
                '2026-07-18 09:00:00',
            ),
        )
        connection.commit()

    command.upgrade(alembic_config, 'head')

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            '''
            SELECT owner_user_id, origin_pair_id, pair_id, question_1
            FROM debriefs
            '''
        ).fetchall()
        table_info = {
            row[1]: row
            for row in connection.execute('PRAGMA table_info(debriefs)')
        }
        indexes = {
            row[1]
            for row in connection.execute('PRAGMA index_list(debriefs)')
        }
        foreign_keys = connection.execute('PRAGMA foreign_key_list(debriefs)').fetchall()

    assert rows == [(owner_id, pair_id, pair_id, '迁移前已有复盘')]
    assert table_info['owner_user_id'][3] == 1
    assert table_info['origin_pair_id'][3] == 0
    assert 'ix_debriefs_owner_user_id' in indexes
    assert 'ix_debriefs_origin_pair_id' in indexes
    assert any(
        row[2] == 'users' and row[3] == 'owner_user_id' and row[4] == 'id'
        for row in foreign_keys
    )
    origin_fk = next(
        row
        for row in foreign_keys
        if row[2] == 'pairs' and row[3] == 'origin_pair_id' and row[4] == 'id'
    )
    display_fk = next(
        row
        for row in foreign_keys
        if row[2] == 'pairs' and row[3] == 'pair_id' and row[4] == 'id'
    )
    assert origin_fk[6] == 'SET NULL'
    assert display_fk[6] == 'SET NULL'


def test_debrief_owner_migration_fails_closed_for_unowned_rows(
    monkeypatch,
    tmp_path,
):
    """无法从 pair 回填的旧复盘必须保留并中止迁移。"""
    database_path = tmp_path / 'debrief-owner-orphan.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    command.downgrade(alembic_config, '0017_delivery_review_workflow')

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''
            INSERT INTO debriefs (
                date,
                community_code,
                pair_id,
                question_1,
                created_at
            ) VALUES (?, ?, NULL, ?, ?)
            ''',
            (
                '2026-07-18',
                '都昌县',
                '归属待人工确认',
                '2026-07-18 09:30:00',
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='orphan_count=1; no rows were deleted'):
        command.upgrade(alembic_config, 'head')

    with sqlite3.connect(database_path) as connection:
        kept = connection.execute(
            '''
            SELECT date, community_code, pair_id, question_1, created_at
            FROM debriefs
            '''
        ).fetchall()
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(debriefs)')
        }
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]

    assert kept == [(
        '2026-07-18',
        '都昌县',
        None,
        '归属待人工确认',
        '2026-07-18 09:30:00',
    )]
    assert 'owner_user_id' not in columns
    assert 'origin_pair_id' not in columns
    assert revision == '0017_delivery_review_workflow'


def test_debrief_owner_migration_refuses_lossy_downgrade_for_opted_out_pair(
    monkeypatch,
    tmp_path,
):
    """关闭家人关联的复盘不能降级到会丢失 owner 的旧结构。"""
    database_path = tmp_path / 'debrief-owner-lossy-downgrade.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.db_models import Pair, User
    from core.extensions import db
    from core.security import hash_short_code
    from core.time_utils import utcnow

    with app.app_context():
        owner = User(username='debrief-downgrade-owner')
        owner.set_password('migration-test-password')
        db.session.add(owner)
        db.session.flush()
        pair = Pair(
            caregiver_id=owner.id,
            community_code='都昌县',
            location_query='都昌县',
            elder_code='debrief-downgrade-elder',
            short_code='82828282',
            short_code_hash=hash_short_code('82828282'),
            status='inactive',
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()
        db.session.commit()
        owner_id = int(owner.id)
        pair_id = int(pair.id)
        db.session.remove()
        db.engine.dispose()

    # 先停在 0018，确保本次断言只检验 owner/origin 降级边界。
    command.downgrade(alembic_config, '0018_debrief_owner_scope')
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''
            INSERT INTO debriefs (
                date,
                community_code,
                owner_user_id,
                origin_pair_id,
                pair_id,
                question_1,
                created_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
            ''',
            (
                '2026-07-18',
                '都昌县',
                owner_id,
                pair_id,
                '已关闭家人关联的复盘',
                '2026-07-18 10:00:00',
            ),
        )
        connection.commit()
    with pytest.raises(
        RuntimeError,
        match='unrepresentable_count=1; owner and origin columns were preserved',
    ):
        command.downgrade(alembic_config, '0017_delivery_review_workflow')

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(debriefs)')
        }
        kept = connection.execute(
            '''
            SELECT owner_user_id, origin_pair_id, pair_id, question_1
            FROM debriefs
            '''
        ).fetchall()
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]

    assert {'owner_user_id', 'origin_pair_id'} <= columns
    assert kept == [(owner_id, pair_id, None, '已关闭家人关联的复盘')]
    assert revision == '0018_debrief_owner_scope'

    command.upgrade(alembic_config, 'head')
    with sqlite3.connect(database_path) as connection:
        restored = connection.execute(
            '''
            SELECT owner_user_id, origin_pair_id, pair_id, question_1
            FROM debriefs
            '''
        ).fetchall()
    assert restored == kept


def test_head_downgrade_preflight_preserves_newer_columns_for_opted_out_pair(
    monkeypatch,
    tmp_path,
):
    """从 head 直接降级时，0021 必须在 0020/0019 删列前拒绝。"""
    from datetime import date

    database_path = tmp_path / 'debrief-head-downgrade-guard.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.db_models import Debrief, Pair, User
    from core.extensions import db
    from core.security import hash_short_code
    from core.time_utils import utcnow

    with app.app_context():
        owner = User(username='debrief-head-guard-owner')
        owner.set_password('migration-test-password')
        db.session.add(owner)
        db.session.flush()
        pair = Pair(
            caregiver_id=owner.id,
            community_code='都昌县',
            location_query='都昌县',
            elder_code='debrief-head-guard-elder',
            short_code='83838383',
            short_code_hash=hash_short_code('83838383'),
            status='inactive',
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()
        db.session.add(Debrief(
            date=date(2026, 7, 18),
            community_code='都昌县',
            owner_user_id=owner.id,
            origin_pair_id=pair.id,
            pair_id=None,
            question_1='head 降级前已关闭关联',
            created_at=utcnow(),
        ))
        db.session.commit()
        owner_id = int(owner.id)
        pair_id = int(pair.id)
        db.session.remove()
        db.engine.dispose()

    with pytest.raises(
        RuntimeError,
        match='unrepresentable_debrief_count=1; head schema was preserved',
    ):
        command.downgrade(alembic_config, '0017_delivery_review_workflow')

    with sqlite3.connect(database_path) as connection:
        debrief_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(debriefs)')
        }
        daily_status_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(daily_status)')
        }
        weather_alert_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(weather_alerts)')
        }
        kept = connection.execute(
            '''
            SELECT owner_user_id, origin_pair_id, pair_id, question_1
            FROM debriefs
            '''
        ).fetchall()
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]

    assert {'owner_user_id', 'origin_pair_id'} <= debrief_columns
    assert 'elder_actions' in daily_status_columns
    assert 'dedupe_key' in weather_alert_columns
    assert kept == [(owner_id, pair_id, None, 'head 降级前已关闭关联')]
    assert revision == '0026_cooling_coordinate_verify'


def test_head_to_0017_round_trip_succeeds_for_representable_debrief(
    monkeypatch,
    tmp_path,
):
    """所有复盘都可由 pair 表达时，完整降级链与重新升级仍应无损。"""
    from datetime import date

    database_path = tmp_path / 'debrief-head-round-trip.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.db_models import Debrief, Pair, User
    from core.extensions import db
    from core.security import hash_short_code
    from core.time_utils import utcnow

    with app.app_context():
        owner = User(username='debrief-round-trip-owner')
        owner.set_password('migration-test-password')
        db.session.add(owner)
        db.session.flush()
        pair = Pair(
            caregiver_id=owner.id,
            community_code='都昌县',
            location_query='都昌县',
            elder_code='debrief-round-trip-elder',
            short_code='84848484',
            short_code_hash=hash_short_code('84848484'),
            status='active',
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()
        db.session.add(Debrief(
            date=date(2026, 7, 18),
            community_code='都昌县',
            owner_user_id=owner.id,
            origin_pair_id=pair.id,
            pair_id=pair.id,
            question_1='可以由旧结构表达',
            created_at=utcnow(),
        ))
        db.session.commit()
        owner_id = int(owner.id)
        pair_id = int(pair.id)
        db.session.remove()
        db.engine.dispose()

    command.downgrade(alembic_config, '0017_delivery_review_workflow')
    with sqlite3.connect(database_path) as connection:
        downgraded_debrief_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(debriefs)')
        }
        downgraded_daily_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(daily_status)')
        }
        downgraded_alert_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(weather_alerts)')
        }
        downgraded_row = connection.execute(
            'SELECT pair_id, question_1 FROM debriefs'
        ).fetchall()
        downgraded_revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]

    assert 'owner_user_id' not in downgraded_debrief_columns
    assert 'origin_pair_id' not in downgraded_debrief_columns
    assert 'elder_actions' not in downgraded_daily_columns
    assert 'dedupe_key' not in downgraded_alert_columns
    assert downgraded_row == [(pair_id, '可以由旧结构表达')]
    assert downgraded_revision == '0017_delivery_review_workflow'

    command.upgrade(alembic_config, 'head')
    with sqlite3.connect(database_path) as connection:
        restored = connection.execute(
            '''
            SELECT owner_user_id, origin_pair_id, pair_id, question_1
            FROM debriefs
            '''
        ).fetchall()
        restored_revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]

    assert restored == [(owner_id, pair_id, pair_id, '可以由旧结构表达')]
    assert restored_revision == '0026_cooling_coordinate_verify'


def test_elder_actions_migration_keeps_caregiver_actions_separate(
    monkeypatch,
    tmp_path,
):
    """老人自护有数据时拒绝降级，清空后仍保留照护端行动数据。"""
    from datetime import date

    database_path = tmp_path / 'elder-actions-separation.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.db_models import DailyStatus, Pair, User
    from core.extensions import db
    from core.security import hash_short_code
    from core.time_utils import utcnow

    with app.app_context():
        owner = User(username='elder-actions-migration-owner')
        owner.set_password('migration-test-password')
        db.session.add(owner)
        db.session.flush()
        pair = Pair(
            caregiver_id=owner.id,
            community_code='都昌县',
            location_query='都昌县',
            elder_code='elder-actions-migration-elder',
            short_code='91919191',
            short_code_hash=hash_short_code('91919191'),
            status='active',
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()
        pair_id = int(pair.id)
        db.session.add(DailyStatus(
            pair_id=pair_id,
            status_date=date(2026, 7, 18),
            community_code='都昌县',
            caregiver_actions='["remind"]',
            elder_actions='["drink_water"]',
        ))
        db.session.commit()
        db.session.remove()
        db.engine.dispose()

    with pytest.raises(
        RuntimeError,
        match='elder_action_count=1; head schema was preserved',
    ):
        command.downgrade(alembic_config, '0018_debrief_owner_scope')

    with sqlite3.connect(database_path) as connection:
        guarded_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(daily_status)')
        }
        guarded_values = connection.execute(
            '''SELECT caregiver_actions, elder_actions
               FROM daily_status WHERE pair_id = ?''',
            (pair_id,),
        ).fetchone()
        guarded_revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]
        connection.execute(
            'UPDATE daily_status SET elder_actions = NULL WHERE pair_id = ?',
            (pair_id,),
        )
        connection.commit()

    assert 'elder_actions' in guarded_columns
    assert guarded_values == ('["remind"]', '["drink_water"]')
    assert guarded_revision == '0026_cooling_coordinate_verify'

    command.downgrade(alembic_config, '0018_debrief_owner_scope')
    with sqlite3.connect(database_path) as connection:
        downgraded_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(daily_status)')
        }
        caregiver_actions = connection.execute(
            'SELECT caregiver_actions FROM daily_status WHERE pair_id = ?',
            (pair_id,),
        ).fetchone()[0]

    assert 'elder_actions' not in downgraded_columns
    assert 'caregiver_actions' in downgraded_columns
    assert caregiver_actions == '["remind"]'

    command.upgrade(alembic_config, 'head')
    with sqlite3.connect(database_path) as connection:
        upgraded_columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(daily_status)')
        }
        caregiver_actions, elder_actions = connection.execute(
            '''
            SELECT caregiver_actions, elder_actions
            FROM daily_status
            WHERE pair_id = ?
            ''',
            (pair_id,),
        ).fetchone()

    assert 'elder_actions' in upgraded_columns
    assert caregiver_actions == '["remind"]'
    assert elder_actions is None


def test_weather_alert_dedupe_migration_refuses_lossy_direct_downgrade(
    monkeypatch,
    tmp_path,
):
    """数据库停在 0020 时，非空幂等键也必须阻止删列。"""
    database_path = tmp_path / 'weather-alert-dedupe-guard.db'
    app = _create_test_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    command.downgrade(alembic_config, '0020_weather_alert_dedupe_key')

    dedupe_key = 'a' * 64
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''
            INSERT INTO weather_alerts (location, alert_type, dedupe_key)
            VALUES (?, ?, ?)
            ''',
            ('都昌县', '高温', dedupe_key),
        )
        connection.commit()

    with pytest.raises(
        RuntimeError,
        match='protected_count=1; dedupe_key was preserved',
    ):
        command.downgrade(alembic_config, '0019_daily_status_elder_actions')

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute('PRAGMA table_info(weather_alerts)')
        }
        kept_key = connection.execute(
            'SELECT dedupe_key FROM weather_alerts'
        ).fetchone()[0]
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]

    assert 'dedupe_key' in columns
    assert kept_key == dedupe_key
    assert revision == '0020_weather_alert_dedupe_key'
