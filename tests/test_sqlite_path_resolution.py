import importlib.util
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
    storage_db = ROOT_DIR / "storage" / "health_weather.db"
    instance_db = ROOT_DIR / "instance" / "health_weather.db"

    storage_exists = storage_db.exists()
    instance_exists = instance_db.exists()
    if storage_exists:
        storage_bytes = storage_db.read_bytes()
        storage_db.unlink()
    if instance_exists:
        instance_bytes = instance_db.read_bytes()
        instance_db.unlink()

    try:
        assert resolve_database_uri() == "sqlite:///health_weather.db"
    finally:
        if storage_exists:
            storage_db.parent.mkdir(parents=True, exist_ok=True)
            storage_db.write_bytes(storage_bytes)
        if instance_exists:
            instance_db.parent.mkdir(parents=True, exist_ok=True)
            instance_db.write_bytes(instance_bytes)


def test_reset_admin_resolves_default_database_uri_to_instance(monkeypatch):
    module = _load_reset_admin_module()
    monkeypatch.setenv("DATABASE_URI", "sqlite:///health_weather.db")
    resolved = module._resolve_db_path(None)
    expected = (Path(module.ROOT_DIR) / "instance" / "health_weather.db").resolve()
    assert resolved == expected
