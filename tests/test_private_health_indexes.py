# -*- coding: utf-8 -*-
"""SQLite 连接性能参数与私有健康查询索引回归。"""

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect


ROOT_DIR = Path(__file__).resolve().parents[1]
INDEX_SPECS = {
    'health_diary': (
        'ix_health_diary_owner_member_date_id',
        ['user_id', 'member_id', 'entry_date', 'id'],
    ),
    'medication_reminders': (
        'ix_medication_owner_member_id',
        ['user_id', 'member_id', 'id'],
    ),
    'health_risk_assessments': (
        'ix_assessment_owner_member_date_id',
        ['user_id', 'member_id', 'assessment_date', 'id'],
    ),
}


def _create_isolated_app(monkeypatch, database_path):
    """创建仅使用临时 SQLite 的应用，不读写开发库。"""
    monkeypatch.setenv('DATABASE_URI', f'sqlite:///{database_path.as_posix()}')
    monkeypatch.setenv('SECRET_KEY', 'private-index-test-secret-key')
    monkeypatch.setenv('PAIR_TOKEN_PEPPER', 'private-index-test-pair-pepper')
    monkeypatch.setenv('DEBUG', 'true')
    monkeypatch.setenv('DEMO_MODE', '1')
    monkeypatch.setenv('RATE_LIMIT_STORAGE_URI', 'memory://')
    monkeypatch.setenv('REDIS_URL', '')
    monkeypatch.setenv('QWEATHER_KEY', '')
    monkeypatch.setenv('QWEATHER_API_BASE', '')
    monkeypatch.setenv('AMAP_KEY', '')
    monkeypatch.setenv('SILICONFLOW_API_KEY', '')

    from core.app import create_app

    return create_app()


def _alembic_config(app):
    config = Config(str(ROOT_DIR / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', app.config['SQLALCHEMY_DATABASE_URI'])
    config.set_main_option('script_location', str(ROOT_DIR / 'migrations'))
    return config


def _assert_index_shapes(database_path):
    from sqlalchemy import create_engine

    engine = create_engine(f'sqlite:///{database_path.as_posix()}')
    try:
        inspector = inspect(engine)
        for table_name, (index_name, expected_columns) in INDEX_SPECS.items():
            indexes = {
                item['name']: item.get('column_names') or []
                for item in inspector.get_indexes(table_name)
            }
            assert indexes[index_name] == expected_columns
    finally:
        engine.dispose()


def test_sqlite_connection_pragmas_cover_file_memory_and_read_only(app, tmp_path):
    """文件库使用 WAL，内存库与只读库仍能安全连接。"""
    from core.db_models import _configure_sqlite_connection
    from core.extensions import db

    with app.app_context():
        with db.engine.connect() as connection:
            assert connection.exec_driver_sql('PRAGMA journal_mode').scalar().lower() == 'wal'
            assert connection.exec_driver_sql('PRAGMA busy_timeout').scalar() == 5000
            assert connection.exec_driver_sql('PRAGMA synchronous').scalar() == 1
            assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1

    memory_connection = sqlite3.connect(':memory:')
    try:
        _configure_sqlite_connection(memory_connection, None)
        assert memory_connection.execute('PRAGMA journal_mode').fetchone()[0] == 'memory'
        assert memory_connection.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
        assert memory_connection.execute('PRAGMA synchronous').fetchone()[0] == 1
        assert memory_connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    finally:
        memory_connection.close()

    read_only_path = tmp_path / 'read-only.db'
    with sqlite3.connect(read_only_path) as writable:
        writable.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY)')
    read_only_connection = sqlite3.connect(
        f'file:{read_only_path.as_posix()}?mode=ro',
        uri=True,
    )
    try:
        _configure_sqlite_connection(read_only_connection, None)
        assert read_only_connection.execute('SELECT COUNT(*) FROM sample').fetchone()[0] == 0
        assert read_only_connection.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    finally:
        read_only_connection.close()


def test_private_health_index_migration_is_idempotent_and_used_by_queries(
    monkeypatch,
    tmp_path,
):
    """0022 允许部分索引已存在，且热查询会命中对应索引。"""
    database_path = tmp_path / 'private-health-indexes.db'
    app = _create_isolated_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    _assert_index_shapes(database_path)
    with sqlite3.connect(database_path) as connection:
        explain_cases = (
            (
                'ix_health_diary_owner_member_date_id',
                '''
                EXPLAIN QUERY PLAN
                SELECT * FROM health_diary
                WHERE user_id = ? AND member_id = ?
                ORDER BY entry_date DESC, id DESC LIMIT 30
                ''',
            ),
            (
                'ix_medication_owner_member_id',
                '''
                EXPLAIN QUERY PLAN
                SELECT * FROM medication_reminders
                WHERE user_id = ? AND member_id = ?
                ORDER BY id DESC LIMIT 50
                ''',
            ),
            (
                'ix_assessment_owner_member_date_id',
                '''
                EXPLAIN QUERY PLAN
                SELECT * FROM health_risk_assessments
                WHERE user_id = ? AND member_id = ?
                ORDER BY assessment_date DESC, id DESC LIMIT 1
                ''',
            ),
        )
        for index_name, statement in explain_cases:
            details = ' '.join(
                str(row[3])
                for row in connection.execute(statement, (7, 11)).fetchall()
            )
            assert index_name in details

    command.downgrade(alembic_config, '0021_debrief_downgrade_guard')
    with sqlite3.connect(database_path) as connection:
        for table_name, (index_name, _columns) in INDEX_SPECS.items():
            existing = {
                row[1]
                for row in connection.execute(f'PRAGMA index_list({table_name})').fetchall()
            }
            assert index_name not in existing
        # 模拟中断过的手工补救：一个索引已存在，其余仍需迁移补齐。
        connection.execute(
            '''
            CREATE INDEX ix_health_diary_owner_member_date_id
            ON health_diary (user_id, member_id, entry_date, id)
            '''
        )

    command.upgrade(alembic_config, 'head')
    command.upgrade(alembic_config, 'head')
    _assert_index_shapes(database_path)
    # 元数据与迁移后的真实结构必须一致，避免只加模型索引或只加迁移。
    command.check(alembic_config)

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]
    assert revision == '0024_wxpusher_consent_receipt'


def test_private_health_index_migration_rejects_missing_table_before_mutation(
    monkeypatch,
    tmp_path,
):
    """目标表损坏时迁移失败，且不会先创建其他索引或推进版本号。"""
    database_path = tmp_path / 'private-health-missing-table.db'
    app = _create_isolated_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    command.downgrade(alembic_config, '0021_debrief_downgrade_guard')

    with sqlite3.connect(database_path) as connection:
        connection.execute('DROP TABLE medication_reminders')
        connection.commit()

    with pytest.raises(RuntimeError, match='missing_tables'):
        command.upgrade(alembic_config, 'head')

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]
        diary_indexes = {
            row[1]
            for row in connection.execute('PRAGMA index_list(health_diary)').fetchall()
        }
        assessment_indexes = {
            row[1]
            for row in connection.execute(
                'PRAGMA index_list(health_risk_assessments)'
            ).fetchall()
        }

    assert revision == '0021_debrief_downgrade_guard'
    assert 'ix_health_diary_owner_member_date_id' not in diary_indexes
    assert 'ix_assessment_owner_member_date_id' not in assessment_indexes


def test_private_health_index_migration_rejects_wrong_same_name_index(
    monkeypatch,
    tmp_path,
):
    """同名索引的列序或唯一性错误时不得盖上迁移版本。"""
    database_path = tmp_path / 'private-health-wrong-index.db'
    app = _create_isolated_app(monkeypatch, database_path)
    initialized = app.test_cli_runner().invoke(args=['init-db'])
    assert initialized.exit_code == 0, initialized.output
    alembic_config = _alembic_config(app)

    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    command.downgrade(alembic_config, '0021_debrief_downgrade_guard')

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            '''
            CREATE UNIQUE INDEX ix_health_diary_owner_member_date_id
            ON health_diary (member_id, user_id, entry_date, id)
            '''
        )
        connection.commit()

    with pytest.raises(RuntimeError, match='invalid_indexes'):
        command.upgrade(alembic_config, 'head')

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            'SELECT version_num FROM alembic_version'
        ).fetchone()[0]
        medication_indexes = {
            row[1]
            for row in connection.execute(
                'PRAGMA index_list(medication_reminders)'
            ).fetchall()
        }

    assert revision == '0021_debrief_downgrade_guard'
    assert 'ix_medication_owner_member_id' not in medication_indexes
