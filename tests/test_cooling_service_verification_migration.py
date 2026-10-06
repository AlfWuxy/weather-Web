"""保证生产存量资源不会被追加迁移误标为已核验。"""
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


def test_upgrade_preserves_coordinates_and_leaves_service_unknown(tmp_path):
    migration = importlib.import_module('migrations.versions.0036_cooling_service_verification')
    engine = sa.create_engine(f'sqlite:///{tmp_path / "cooling.db"}')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE pairs (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE cooling_resources (id INTEGER PRIMARY KEY, name TEXT, coordinate_verified_at DATETIME, is_active BOOLEAN)')
        conn.exec_driver_sql("INSERT INTO cooling_resources VALUES (1, '存量避暑点', '2026-10-01', 1)")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
        row = conn.execute(sa.text('SELECT name, coordinate_verified_at, is_active, last_verified_at, verify_status FROM cooling_resources')).one()
        assert tuple(row) == ('存量避暑点', '2026-10-01', 1, None, None)
        assert {i['name'] for i in sa.inspect(conn).get_indexes('cooling_feedback')} == {
            f'ix_cooling_feedback_{column}' for column in ('resource_id', 'pair_id', 'code', 'created_at')}
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert conn.execute(sa.text('SELECT name FROM cooling_resources')).scalar_one() == '存量避暑点'
        assert 'cooling_feedback' in sa.inspect(conn).get_table_names()
    engine.dispose()


def test_precreated_model_schema_is_accepted(app):
    from core.extensions import db
    migration = importlib.import_module('migrations.versions.0036_cooling_service_verification')
    with app.app_context():
        db.create_all()
        with db.engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()


def test_wrong_verification_column_is_rejected(tmp_path):
    migration = importlib.import_module('migrations.versions.0036_cooling_service_verification')
    engine = sa.create_engine(f'sqlite:///{tmp_path / "wrong.db"}')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE pairs (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE cooling_resources (id INTEGER PRIMARY KEY, last_verified_at INTEGER)')
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match='结构不兼容'):
                migration.upgrade()
    engine.dispose()
