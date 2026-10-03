"""生产维护脚本的临时目录回归；不访问生产服务或真实数据库。"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_helper(name, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_quick_fix_keeps_secrets_private_without_replacing_existing_values(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("quick_fix.sh", "secure_environment.py", "update_env_value.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    first = subprocess.run(["bash", str(scripts / "quick_fix.sh")], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    env_file = tmp_path / ".env"
    before = env_file.read_text()
    env_file.chmod(0o666)
    second = subprocess.run(["bash", str(scripts / "quick_fix.sh")], capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert env_file.read_text() == before
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "instance").stat().st_mode & 0o777 == 0o700
    for line in before.splitlines():
        assert line.partition("=")[2] not in first.stdout + second.stdout



def test_quick_fix_preserves_production_persistent_instance_link(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("quick_fix.sh", "secure_environment.py", "update_env_value.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    persistent = tmp_path / "persistent-state"
    persistent.mkdir(mode=0o750)
    (tmp_path / "instance").symlink_to(persistent, target_is_directory=True)
    result = subprocess.run(["bash", str(scripts / "quick_fix.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "instance").is_symlink()
    assert persistent.stat().st_mode & 0o777 == 0o750
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600

def test_env_reuses_native_atomic_writer_with_private_file_before_secret_write(tmp_path, monkeypatch):
    helper = load_helper("secure_environment", monkeypatch)
    import update_env_value
    target = tmp_path / ".env"
    target.write_text("export SECRET_KEY = old\nSECRET_KEY=duplicate\nUNRELATED=kept\n")
    target.chmod(0o666)
    original = update_env_value.tempfile.mkstemp
    observed = []
    def mkstemp(**kwargs):
        fd, name = original(**kwargs)
        observed.append((target.stat().st_mode & 0o777, os.fstat(fd).st_mode & 0o777))
        return fd, name
    monkeypatch.setattr(update_env_value.tempfile, "mkstemp", mkstemp)
    helper.update_values(target, {"SECRET_KEY": "replacement"})
    assert observed == [(0o600, 0o600)]
    assert target.read_text() == "UNRELATED=kept\nSECRET_KEY=replacement\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".env.tmp.*"))


@pytest.mark.parametrize("kind", ["symbolic", "hard"])
def test_env_protection_rejects_links_without_changing_target(tmp_path, monkeypatch, kind):
    helper = load_helper("secure_environment", monkeypatch)
    original = tmp_path / "unrelated"
    original.write_text("ORIGINAL=value\n")
    original.chmod(0o644)
    path = tmp_path / ".env"
    path.symlink_to(original) if kind == "symbolic" else os.link(original, path)
    with pytest.raises((OSError, ValueError)):
        helper.update_values(path, {"SECRET_KEY": "replacement"})
    assert original.stat().st_mode & 0o777 == 0o644
    assert original.read_text() == "ORIGINAL=value\n"


@pytest.mark.parametrize("wrong_owner", ["directory", "archive"])
def test_backup_wrong_owner_is_rejected_for_any_execution_account(tmp_path, monkeypatch, wrong_owner):
    helper = load_helper("backup_privacy", monkeypatch)
    root = tmp_path / "backups"
    root.mkdir()
    archive = root / "older.db.gz"
    archive.write_bytes(b"synthetic archive")
    target = root if wrong_owner == "directory" else archive
    original = Path.lstat
    def lstat(path):
        info = original(path)
        if path == target:
            return SimpleNamespace(st_uid=os.geteuid() + 1000, st_mode=info.st_mode, st_nlink=info.st_nlink)
        return info
    monkeypatch.setattr(helper.Path, "lstat", lstat)
    with pytest.raises(ValueError, match="属主"):
        helper.protect_directory(root)
    assert archive.read_bytes() == b"synthetic archive"


def test_backup_privacy_allows_current_nonroot_owner_and_repairs_modes(tmp_path, monkeypatch):
    helper = load_helper("backup_privacy", monkeypatch)
    root = tmp_path / "backups"
    root.mkdir(mode=0o755)
    archive = root / "older.db.gz"
    archive.write_bytes(b"synthetic archive")
    archive.chmod(0o644)
    helper.protect_directory(root)
    assert root.stat().st_uid == os.geteuid()
    assert root.stat().st_mode & 0o777 == 0o700
    assert archive.stat().st_mode & 0o777 == 0o600


def test_backup_privacy_rejects_linked_archives(tmp_path, monkeypatch):
    helper = load_helper("backup_privacy", monkeypatch)
    root = tmp_path / "backups"
    root.mkdir()
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"synthetic")
    outside.chmod(0o644)
    (root / "alias.db").symlink_to(outside)
    with pytest.raises(ValueError, match="链接"):
        helper.protect_directory(root)
    assert outside.stat().st_mode & 0o777 == 0o644
