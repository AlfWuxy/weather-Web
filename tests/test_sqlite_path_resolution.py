import gzip
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import resolve_database_uri, resolve_sqlite_db_path


def _load_reset_admin_module():
    module_path = ROOT_DIR / "scripts" / "reset_admin.py"
    spec = importlib.util.spec_from_file_location("reset_admin_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_resolve_sqlite_db_path_matches_instance_path():
    db_path = resolve_sqlite_db_path("sqlite:///health_weather.db", ROOT_DIR)
    assert db_path == (ROOT_DIR / "instance" / "health_weather.db").resolve()


def test_resolve_database_uri_defaults_to_relative_instance_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URI", raising=False)
    assert resolve_database_uri() in {
        "sqlite:///health_weather.db",
        f"sqlite:///{(ROOT_DIR / 'storage' / 'health_weather.db').as_posix()}",
        f"sqlite:///{(ROOT_DIR / 'instance' / 'health_weather.db').as_posix()}",
    }


def test_reset_admin_resolves_default_database_uri_to_instance(monkeypatch):
    module = _load_reset_admin_module()
    monkeypatch.setenv("DATABASE_URI", "sqlite:///health_weather.db")
    resolved = module._resolve_db_path(None)
    expected = (Path(module.ROOT_DIR) / "instance" / "health_weather.db").resolve()
    assert resolved == expected


def test_backup_script_parses_relative_sqlite_uri_to_instance_path():
    script_path = ROOT_DIR / "scripts" / "backup.sh"
    command = (
        f"source '{script_path}' >/dev/null 2>&1; "
        f"PROJECT_DIR='{ROOT_DIR}'; "
        "parse_sqlite_path 'sqlite:///health_weather.db'"
    )
    result = subprocess.run(
        ["bash", "-lc", command],
        check=True,
        capture_output=True,
        text=True,
    )
    expected = str((ROOT_DIR / "instance" / "health_weather.db").resolve())
    assert result.stdout.strip() == expected


def test_backup_script_resolves_release_instance_symlink(tmp_path):
    script_path = ROOT_DIR / "scripts" / "backup.sh"
    persistent_instance = tmp_path / "persistent" / "instance"
    persistent_instance.mkdir(parents=True)
    release_app = tmp_path / "releases" / "candidate" / "app"
    release_app.mkdir(parents=True)
    (release_app / "instance").symlink_to(persistent_instance, target_is_directory=True)

    command = (
        f"source '{script_path}'; "
        f"PROJECT_DIR='{release_app}'; "
        "parse_sqlite_path 'sqlite:///health_weather.db'"
    )
    result = subprocess.run(
        ["bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == str(persistent_instance / "health_weather.db")


def _backup_env(**overrides):
    env = os.environ.copy()
    env.pop("DATABASE_URI", None)
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def test_backup_project_dir_does_not_follow_external_env_file(tmp_path):
    script_path = ROOT_DIR / "scripts" / "backup.sh"
    env_file = tmp_path / "external" / ".env"
    result = subprocess.run(
        ["bash", "-c", f"source '{script_path}'; printf '%s' \"$PROJECT_DIR\""],
        check=True,
        capture_output=True,
        text=True,
        env=_backup_env(ENV_FILE=env_file),
    )
    assert result.stdout == str(ROOT_DIR)


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("sqlite:///health_weather.db", "/srv/case/instance/health_weather.db"),
        ("sqlite:///storage/live.db?mode=ro", "/srv/case/storage/live.db"),
        ("sqlite:////var/lib/case/live.db", "/var/lib/case/live.db"),
        ("sqlite+pysqlite:///health_weather.db", "/srv/case/instance/health_weather.db"),
        ("sqlite+pysqlite:////var/lib/case/live.db?timeout=10", "/var/lib/case/live.db"),
    ],
)
def test_backup_script_sqlite_uri_matrix(uri, expected):
    script_path = ROOT_DIR / "scripts" / "backup.sh"
    command = f"source '{script_path}'; PROJECT_DIR=/srv/case; parse_sqlite_path '{uri}'"
    result = subprocess.run(["bash", "-c", command], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == expected


def test_backup_script_rejects_non_sqlite_uri(tmp_path):
    script_path = ROOT_DIR / "scripts" / "backup.sh"
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URI=postgresql://db.example/live\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(script_path), "--if-present"],
        capture_output=True,
        text=True,
        env=_backup_env(PROJECT_DIR=tmp_path, ENV_FILE=env_file),
    )
    assert result.returncode == 2
    assert "仅支持 sqlite" in result.stderr


def test_backup_script_missing_source_fails_unless_if_present(tmp_path):
    script_path = ROOT_DIR / "scripts" / "backup.sh"
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URI='sqlite:///missing.db' # 首次部署\n", encoding="utf-8")
    env = _backup_env(PROJECT_DIR=tmp_path, ENV_FILE=env_file)

    required = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, env=env)
    optional = subprocess.run(
        ["bash", str(script_path), "--if-present"], capture_output=True, text=True, env=env
    )

    assert required.returncode == 3
    assert "拒绝创建空备份" in required.stderr
    assert optional.returncode == 0
    assert "按 --if-present 跳过备份" in optional.stdout
    assert not (tmp_path / "backups").exists()


def test_backup_script_opens_source_database_read_only():
    content = (ROOT_DIR / "scripts" / "backup.sh").read_text(encoding="utf-8")

    assert '-readonly "$DB_FILE"' in content
    assert 'sqlite3 "$DB_FILE" ".backup' not in content
    assert '/tmp/case-weather-backup.XXXXXX' in content
    assert '"$runuser_path" -u "$BACKUP_RUNTIME_USER" --' in content
    assert '"$install_path" -m 0600 "$STAGING_FILE" "$BACKUP_FILE"' in content
    assert '-name "health_weather_*.db.gz"' in content
    assert '-name "*.gz"' not in content


def test_managed_backup_requires_authoritative_database_file(tmp_path):
    script_path = ROOT_DIR / "scripts" / "backup.sh"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URI=sqlite:///instance/wrong.db\n",
        encoding="utf-8",
    )
    command = f"source '{script_path}'; id() {{ printf '0\\n'; }}; main --if-present"

    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env=_backup_env(
            PROJECT_DIR=tmp_path,
            ENV_FILE=env_file,
            BACKUP_RUNTIME_USER="case-weather-test",
            BACKUP_DATABASE_FILE="",
        ),
        check=False,
    )

    assert result.returncode == 2
    assert "必须显式提供 BACKUP_DATABASE_FILE" in result.stderr
    assert not (tmp_path / "backups").exists()


def test_managed_backup_stages_as_runtime_user_and_finalizes_root_copy(tmp_path):
    sqlite3_binary = shutil.which("sqlite3")
    install_binary = shutil.which("install")
    mktemp_binary = shutil.which("mktemp")
    if not all((sqlite3_binary, install_binary, mktemp_binary)):
        pytest.skip("本机缺少 SQLite 备份行为测试所需命令")

    script_path = ROOT_DIR / "scripts" / "backup.sh"
    project_dir = tmp_path / "project"
    storage_dir = project_dir / "storage"
    instance_dir = project_dir / "instance"
    backup_dir = project_dir / "backups" / "daily"
    storage_dir.mkdir(parents=True)
    instance_dir.mkdir(parents=True)
    database_file = storage_dir / "live.db"
    connection = sqlite3.connect(database_file)
    try:
        connection.execute("CREATE TABLE release_state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO release_state(value) VALUES ('managed')")
        connection.commit()
    finally:
        connection.close()
    wrong_database = instance_dir / "wrong.db"
    wrong_connection = sqlite3.connect(wrong_database)
    try:
        wrong_connection.execute("CREATE TABLE release_state (value TEXT NOT NULL)")
        wrong_connection.execute("INSERT INTO release_state(value) VALUES ('wrong')")
        wrong_connection.commit()
    finally:
        wrong_connection.close()
    env_file = project_dir / ".env"
    env_file.write_text("DATABASE_URI=sqlite:///instance/wrong.db\n", encoding="utf-8")
    backup_dir.mkdir(parents=True)
    retained_archive = backup_dir / "retained-old.db.gz"
    retained_archive.write_bytes(b"retained")
    old_timestamp = retained_archive.stat().st_mtime - 40 * 24 * 60 * 60
    os.utime(retained_archive, (old_timestamp, old_timestamp))

    runuser_log = tmp_path / "runuser.log"
    fake_runuser = tmp_path / "runuser"
    fake_runuser.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_RUNUSER_LOG"
[ "$1" = '-u' ] || exit 90
shift 2
[ "$1" = '--' ] || exit 91
shift
exec "$@"
""",
        encoding="utf-8",
    )
    fake_runuser.chmod(0o755)
    command = f"source '{script_path}'; id() {{ printf '0\\n'; }}; main --if-present"
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env=_backup_env(
            PROJECT_DIR=project_dir,
            ENV_FILE=env_file,
            BACKUP_DIR=backup_dir,
            DEFAULT_DB_FILE=project_dir / "instance" / "health_weather.db",
            BACKUP_RUNTIME_USER="case-weather-test",
            BACKUP_DATABASE_FILE=database_file,
            BACKUP_PRUNE="0",
            RUNUSER_BIN=fake_runuser,
            SQLITE3_BIN=sqlite3_binary,
            MKTEMP_BIN=mktemp_binary,
            INSTALL_BIN=install_binary,
            FAKE_RUNUSER_LOG=runuser_log,
        ),
        check=False,
    )

    assert result.returncode == 0, result.stderr
    archives = list(backup_dir.glob("health_weather_*.db.gz"))
    assert len(archives) == 1
    assert retained_archive.exists()
    restored = tmp_path / "restored.db"
    with gzip.open(archives[0], "rb") as source, restored.open("wb") as destination:
        shutil.copyfileobj(source, destination)
    restored_connection = sqlite3.connect(restored)
    try:
        value = restored_connection.execute("SELECT value FROM release_state").fetchone()[0]
    finally:
        restored_connection.close()
    assert value == "managed"
    log_text = runuser_log.read_text(encoding="utf-8")
    assert "-u case-weather-test --" in log_text
    assert "-readonly" in log_text
