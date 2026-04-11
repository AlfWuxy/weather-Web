import importlib.util
import subprocess
import sys
from pathlib import Path

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
