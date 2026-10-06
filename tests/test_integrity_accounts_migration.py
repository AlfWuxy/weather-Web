"""生产 0034 的兼容追加迁移，不触碰真实数据库。"""
import importlib
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def run_migration(connection, monkeypatch):
    migration = importlib.import_module('migrations.versions.0035_integrity_accounts')
    monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(connection)))
    migration.upgrade()


def test_preserves_production_identity_and_existing_consent(monkeypatch):
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as c:
        c.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(120), auth_version INTEGER NOT NULL, deleted_at DATETIME, health_sensitive_consent_version VARCHAR(64), health_sensitive_consented_at DATETIME)'))
        c.execute(sa.text('CREATE TABLE family_members (id INTEGER PRIMARY KEY, user_id INTEGER, name VARCHAR(50))'))
        c.execute(sa.text("INSERT INTO users VALUES (1, 'existing@example.test', 9, NULL, 'old-version', '2026-01-01')"))
        c.execute(sa.text("INSERT INTO family_members VALUES (1, 1, 'sample')"))
        run_migration(c, monkeypatch)
        run_migration(c, monkeypatch)
        row = c.execute(sa.text('SELECT * FROM users')).mappings().one()
        assert row['auth_version'] == 9 and row['health_sensitive_consent_version'] == 'old-version'
        assert row['email_verified_at'] is None and row['deleted_at'] is None
        member = c.execute(sa.text('SELECT * FROM family_members')).mappings().one()
        assert member['health_sensitive_consented_at'] is None
        assert member['health_sensitive_consent_version'] is None
        assert 'closed_at' not in row


def test_accepts_fresh_create_all_schema(app, db_session, monkeypatch):
    from core.extensions import db
    with db.engine.begin() as connection:
        run_migration(connection, monkeypatch)


def test_rejects_incompatible_preexisting_email_column(monkeypatch):
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as c:
        c.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, email_verified_at INTEGER NOT NULL)'))
        c.execute(sa.text('CREATE TABLE family_members (id INTEGER PRIMARY KEY)'))
        with pytest.raises(RuntimeError, match='列结构不兼容'):
            run_migration(c, monkeypatch)
