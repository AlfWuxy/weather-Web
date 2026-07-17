# -*- coding: utf-8 -*-
"""空数据库初始化链路回归测试。"""
from pathlib import Path

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
