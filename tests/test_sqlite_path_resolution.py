import importlib.util
import os
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
