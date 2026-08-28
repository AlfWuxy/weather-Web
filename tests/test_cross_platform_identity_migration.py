# -*- coding: utf-8 -*-
"""0027 跨端身份迁移的结构、冲突与降级保护。"""

import importlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex


ROOT_DIR = Path(__file__).resolve().parents[1]
HEAD_REVISION = "0032_weather_alert_provenance"
PREVIOUS_REVISION = "0026_cooling_coordinate_verify"


def _create_app(monkeypatch, database_path):
    monkeypatch.setenv("DATABASE_URI", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("SECRET_KEY", "cross-platform-migration-secret")
    monkeypatch.setenv("PAIR_TOKEN_PEPPER", "cross-platform-pair-pepper")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "memory://")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("QWEATHER_KEY", "")
    monkeypatch.setenv("QWEATHER_API_BASE", "")
    monkeypatch.setenv("AMAP_KEY", "")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "")
    monkeypatch.setenv("SENTRY_DSN", "")

    from core.app import create_app

    return create_app()


def _initialize(monkeypatch, database_path):
    app = _create_app(monkeypatch, database_path)
    result = app.test_cli_runner().invoke(args=["init-db"])
    assert result.exit_code == 0, result.output
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        app.config["SQLALCHEMY_DATABASE_URI"],
    )
    config.set_main_option("script_location", str(ROOT_DIR / "migrations"))
    from core.extensions import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    return config


def _revision(connection):
    return connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()[0]


def test_cross_platform_identity_migration_round_trip_builds_expected_schema(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / "cross-platform-round-trip.db"
    config = _initialize(monkeypatch, database_path)

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == PREVIOUS_REVISION
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        identity_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(miniprogram_identities)"
            )
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        user_indexes_before_upgrade = {
            row[1] for row in connection.execute("PRAGMA index_list(users)")
        }
    assert "phone_normalized" not in user_columns
    assert "phone_verified_at" not in user_columns
    assert "account_origin" not in user_columns
    assert "ix_users_phone_normalized" not in user_indexes_before_upgrade
    assert (
        "uq_users_verified_phone_normalized"
        not in user_indexes_before_upgrade
    )
    assert "link_failed_count" not in identity_columns
    assert "binding_auth_version" not in identity_columns
    assert "miniprogram_link_challenges" not in tables

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == HEAD_REVISION
        user_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(users)")
        }
        identity_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(miniprogram_identities)"
            )
        }
        challenge_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(miniprogram_link_challenges)"
            )
        }
        user_indexes = {
            row[1]: row
            for row in connection.execute("PRAGMA index_list(users)")
        }
        user_index_sql = {
            row[0]: row[1]
            for row in connection.execute(
                """SELECT name, sql FROM sqlite_master
                   WHERE type = 'index' AND tbl_name = 'users'"""
            )
        }
        identity_indexes = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA index_list(miniprogram_identities)"
            )
        }
        challenge_indexes = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA index_list(miniprogram_link_challenges)"
            )
        }

    assert user_columns["phone_normalized"][2].upper() == "VARCHAR(32)"
    assert user_columns["phone_normalized"][3] == 0
    assert user_columns["phone_verified_at"][3] == 0
    assert user_columns["account_origin"][2].upper() == "VARCHAR(32)"
    assert user_columns["account_origin"][3] == 1
    assert user_columns["account_origin"][4].strip("'\"") == "web"
    assert identity_columns["link_failed_count"][2].upper() == "INTEGER"
    assert identity_columns["link_failed_count"][3] == 1
    assert identity_columns["binding_auth_version"][2].upper() == "INTEGER"
    assert identity_columns["binding_auth_version"][3] == 1
    assert challenge_columns["auth_version_at_create"][3] == 1
    assert user_indexes["ix_users_phone_normalized"][2] == 0
    assert "uq_users_phone_normalized" not in user_indexes
    assert user_indexes["uq_users_verified_phone_normalized"][2] == 1
    verified_phone_index_sql = (
        user_index_sql["uq_users_verified_phone_normalized"]
        .replace('"', '')
        .upper()
    )
    assert "WHERE PHONE_VERIFIED_AT IS NOT NULL" in verified_phone_index_sql
    assert identity_indexes["uq_miniprogram_identities_user_id"][2] == 1
    assert challenge_indexes["uq_mp_link_active_user"][2] == 1

    with sqlite3.connect(database_path) as connection:
        pending_ids = []
        for username in ("pending-phone-a", "pending-phone-b"):
            pending_ids.append(connection.execute(
                """INSERT INTO users (
                       username, password_hash, role, auth_version,
                       phone_normalized
                   ) VALUES (?, 'hash', 'user', 1, '+8613800138000')""",
                (username,),
            ).lastrowid)
        connection.commit()
        pending_count = connection.execute(
            """SELECT COUNT(*) FROM users
               WHERE phone_normalized = '+8613800138000'
                 AND phone_verified_at IS NULL"""
        ).fetchone()[0]
        connection.execute(
            """UPDATE users
               SET phone_verified_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (pending_ids[0],),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """UPDATE users
                   SET phone_verified_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (pending_ids[1],),
            )
        connection.rollback()
    assert pending_count == 2


def test_cross_platform_identity_migration_backfills_binding_versions(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / "cross-platform-version-backfill.db"
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        user_id = connection.execute(
            """INSERT INTO users (
                   username, password_hash, role, auth_version
               ) VALUES ('version-backfill-user', 'hash', 'user', 3)"""
        ).lastrowid
        connection.execute(
            """INSERT INTO miniprogram_identities (
                   user_id, openid_hash, privacy_consent_version,
                   privacy_consented_at, acquisition_source
               ) VALUES (?, ?, 'privacy-v1', CURRENT_TIMESTAMP, 'direct')""",
            (user_id, "c" * 64),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        account_origin = connection.execute(
            "SELECT account_origin FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()[0]
        binding_auth_version = connection.execute(
            """SELECT binding_auth_version
               FROM miniprogram_identities
               WHERE user_id = ?""",
            (user_id,),
        ).fetchone()[0]

    assert account_origin == "web"
    assert binding_auth_version == 3


def test_cross_platform_identity_migration_recovers_only_proven_legacy_placeholders(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / "cross-platform-origin-backfill.db"
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        proven_hash = "1" * 64
        proven_id = connection.execute(
            """INSERT INTO users (
                   username, password_hash, role, auth_version
               ) VALUES (?, 'hash', 'user', 1)""",
            (f"wx_{proven_hash[:24]}",),
        ).lastrowid
        connection.execute(
            """INSERT INTO miniprogram_identities (
                   user_id, openid_hash, privacy_consent_version,
                   privacy_consented_at, acquisition_source
               ) VALUES (?, ?, 'privacy-v1', CURRENT_TIMESTAMP, 'direct')""",
            (proven_id, proven_hash),
        )

        real_web_hash = "2" * 64
        real_web_id = connection.execute(
            """INSERT INTO users (
                   username, password_hash, role, auth_version
               ) VALUES ('wx_real_web_user', 'hash', 'user', 1)"""
        ).lastrowid
        connection.execute(
            """INSERT INTO miniprogram_identities (
                   user_id, openid_hash, privacy_consent_version,
                   privacy_consented_at, acquisition_source
               ) VALUES (?, ?, 'privacy-v1', CURRENT_TIMESTAMP, 'direct')""",
            (real_web_id, real_web_hash),
        )

        private_hash = "3" * 64
        private_id = connection.execute(
            """INSERT INTO users (
                   username, password_hash, role, auth_version
               ) VALUES (?, 'hash', 'user', 1)""",
            (f"wx_{private_hash[:24]}",),
        ).lastrowid
        connection.execute(
            """INSERT INTO miniprogram_identities (
                   user_id, openid_hash, privacy_consent_version,
                   privacy_consented_at, acquisition_source
               ) VALUES (?, ?, 'privacy-v1', CURRENT_TIMESTAMP, 'direct')""",
            (private_id, private_hash),
        )
        connection.execute(
            """INSERT INTO family_members (
                   user_id, name, relation
               ) VALUES (?, '已有家人', '家人')""",
            (private_id,),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        origins = dict(connection.execute(
            """SELECT id, account_origin
               FROM users
               WHERE id IN (?, ?, ?)""",
            (proven_id, real_web_id, private_id),
        ))

    assert origins[proven_id] == "miniprogram_placeholder"
    assert origins[real_web_id] == "web"
    assert origins[private_id] == "web"


def test_cross_platform_identity_clean_downgrade_drops_new_origin_columns(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / "cross-platform-clean-downgrade.db"
    config = _initialize(monkeypatch, database_path)

    command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        identity_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(miniprogram_identities)"
            )
        }

    assert "account_origin" not in user_columns
    assert "binding_auth_version" not in identity_columns


def test_verified_phone_partial_unique_index_compiles_for_postgresql():
    from core.db_models import User

    index = next(
        item
        for item in User.__table__.indexes
        if item.name == "uq_users_verified_phone_normalized"
    )
    ddl = str(
        CreateIndex(index).compile(dialect=postgresql.dialect())
    ).upper()

    assert "CREATE UNIQUE INDEX" in ddl
    assert "WHERE PHONE_VERIFIED_AT IS NOT NULL" in ddl


def test_upgrade_repairs_legacy_full_unique_active_challenge_index(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / "legacy-full-challenge-index.db"
    config = _initialize(monkeypatch, database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX uq_mp_link_active_user")
        connection.execute(
            """CREATE UNIQUE INDEX uq_mp_link_active_user
               ON miniprogram_link_challenges (user_id)"""
        )
        connection.execute(
            "UPDATE alembic_version SET version_num = ?",
            (PREVIOUS_REVISION,),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        user_id = connection.execute(
            """INSERT INTO users (username, password_hash, role, auth_version)
               VALUES ('legacy-challenge-index', 'hash', 'user', 1)"""
        ).lastrowid
        connection.execute(
            """INSERT INTO miniprogram_link_challenges (
                   user_id, code_hash, created_at, expires_at,
                   auth_version_at_create, consumed_at, attempt_count
               ) VALUES (
                   ?, ?, CURRENT_TIMESTAMP, '2099-01-01', 1,
                   CURRENT_TIMESTAMP, 1
               )""",
            (user_id, "a" * 64),
        )
        connection.execute(
            """INSERT INTO miniprogram_link_challenges (
                   user_id, code_hash, created_at, expires_at,
                   auth_version_at_create, attempt_count
               ) VALUES (?, ?, CURRENT_TIMESTAMP, '2099-01-01', 1, 0)""",
            (user_id, "b" * 64),
        )
        connection.commit()
        index_sql = connection.execute(
            """SELECT sql FROM sqlite_master
               WHERE type = 'index' AND name = 'uq_mp_link_active_user'"""
        ).fetchone()[0]
        revision = _revision(connection)

    assert revision == HEAD_REVISION
    normalized_index_sql = index_sql.replace('"', "").upper()
    assert "WHERE CONSUMED_AT IS NULL AND REVOKED_AT IS NULL" in (
        normalized_index_sql
    )


@pytest.mark.parametrize(
    "preflight_name",
    ("_preflight_upgrade", "_preflight_downgrade"),
)
def test_cross_platform_identity_preflight_rejects_unknown_partial_index_dialect(
    preflight_name,
):
    migration = importlib.import_module(
        "migrations.versions.0027_cross_platform_identity"
    )
    bind = SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"),
    )

    with pytest.raises(
        RuntimeError,
        match="unsupported_partial_index_dialect=mysql",
    ):
        getattr(migration, preflight_name)(bind, inspector=None)


def test_upgrade_rejects_duplicate_verified_phone_before_migration_ddl(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / "duplicate-verified-phone.db"
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE users ADD COLUMN phone_normalized VARCHAR(32)"
        )
        connection.execute(
            "ALTER TABLE users ADD COLUMN phone_verified_at DATETIME"
        )
        for username in ("verified-phone-a", "verified-phone-b"):
            connection.execute(
                """INSERT INTO users (
                       username, password_hash, role, auth_version,
                       phone_normalized, phone_verified_at
                   ) VALUES (
                       ?, 'hash', 'user', 1, '+8613900000001',
                       CURRENT_TIMESTAMP
                   )""",
                (username,),
            )
        connection.commit()

    with pytest.raises(
        RuntimeError,
        match="duplicate_verified_phone_count=2",
    ):
        command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == PREVIOUS_REVISION
        identity_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(miniprogram_identities)"
            )
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        user_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(users)")
        }
    assert "link_failed_count" not in identity_columns
    assert "miniprogram_link_challenges" not in tables
    assert "ix_users_phone_normalized" not in user_indexes
    assert "uq_users_verified_phone_normalized" not in user_indexes


@pytest.mark.parametrize("guard_case", ("phone", "challenge"))
def test_cross_platform_identity_downgrade_refuses_new_identity_data(
    monkeypatch,
    tmp_path,
    guard_case,
):
    database_path = tmp_path / f"cross-platform-guard-{guard_case}.db"
    config = _initialize(monkeypatch, database_path)

    with sqlite3.connect(database_path) as connection:
        user_id = connection.execute(
            """INSERT INTO users (username, password_hash, role, auth_version)
               VALUES ('cross-platform-guard', 'hash', 'user', 1)"""
        ).lastrowid
        if guard_case == "phone":
            connection.execute(
                "UPDATE users SET phone_normalized = ? WHERE id = ?",
                ("+8613800138000", user_id),
            )
        else:
            connection.execute(
                """INSERT INTO miniprogram_link_challenges (
                       user_id, code_hash, created_at, expires_at,
                       auth_version_at_create, attempt_count
                   ) VALUES (?, ?, CURRENT_TIMESTAMP, '2099-01-01', 1, 0)""",
                (user_id, "a" * 64),
            )
        connection.commit()

    expected = (
        "phone_identity_count=1"
        if guard_case == "phone"
        else "link_challenge_count=1"
    )
    with pytest.raises(RuntimeError, match=expected):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == HEAD_REVISION
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "phone_normalized" in user_columns
    assert "miniprogram_link_challenges" in tables


@pytest.mark.parametrize("conflict_case", ("phone_username", "duplicate_identity"))
def test_cross_platform_identity_upgrade_rejects_ambiguous_history_before_ddl(
    monkeypatch,
    tmp_path,
    conflict_case,
):
    database_path = tmp_path / f"cross-platform-conflict-{conflict_case}.db"
    config = _initialize(monkeypatch, database_path)
    command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        username = (
            "13800138000"
            if conflict_case == "phone_username"
            else "duplicate-identity-owner"
        )
        user_id = connection.execute(
            """INSERT INTO users (username, password_hash, role, auth_version)
               VALUES (?, 'hash', 'user', 1)""",
            (username,),
        ).lastrowid
        if conflict_case == "duplicate_identity":
            for index in range(2):
                connection.execute(
                    """INSERT INTO miniprogram_identities (
                           user_id, openid_hash, privacy_consent_version,
                           privacy_consented_at, acquisition_source
                       ) VALUES (?, ?, 'privacy-v1', CURRENT_TIMESTAMP, 'direct')""",
                    (user_id, f"{index + 1:064x}"),
                )
        connection.commit()

    expected = (
        "phone_shaped_username_ids"
        if conflict_case == "phone_username"
        else "duplicate_identity_user_id"
    )
    with pytest.raises(RuntimeError, match=expected):
        command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        assert _revision(connection) == PREVIOUS_REVISION
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        identity_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(miniprogram_identities)"
            )
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "phone_normalized" not in user_columns
    assert "link_failed_count" not in identity_columns
    assert "miniprogram_link_challenges" not in tables
