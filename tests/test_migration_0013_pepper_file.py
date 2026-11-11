# -*- coding: utf-8 -*-
"""0013 读取持久化 pepper 时不跟随符号链接，并拒绝过宽权限。"""
import importlib.util
import os
import stat
from pathlib import Path

import pytest
import sqlalchemy as sa


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / 'migrations'
    / 'versions'
    / '0013_daybreak_security_hardening.py'
)


@pytest.fixture
def migration():
    spec = importlib.util.spec_from_file_location('migration_0013_pepper', _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root(monkeypatch, migration, root):
    instance = root / 'instance'
    instance.mkdir()
    monkeypatch.setattr(migration, '_repo_root', lambda: str(root))
    return instance


def _write_pepper(instance, content, mode=0o600):
    path = instance / 'pair_token_pepper.txt'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, content.encode('utf-8'))
    finally:
        os.close(fd)
    os.chmod(path, mode)
    return path


def _clear_pepper_env(monkeypatch):
    monkeypatch.delenv('PAIR_TOKEN_PEPPER', raising=False)
    monkeypatch.delenv('SECRET_KEY', raising=False)
    monkeypatch.setenv('DEBUG', 'true')


def test_private_file_is_read(monkeypatch, migration, tmp_path):
    instance = _root(monkeypatch, migration, tmp_path)
    _write_pepper(instance, '  stored-pepper \n')
    assert migration._read_persisted_pair_token_pepper() == 'stored-pepper'


def test_open_uses_nofollow(monkeypatch, migration, tmp_path):
    instance = _root(monkeypatch, migration, tmp_path)
    _write_pepper(instance, 'stored-pepper')
    real_open = migration.os.open
    seen = {}

    def tracking_open(path, flags, *args, **kwargs):
        seen['flags'] = flags
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(migration.os, 'open', tracking_open)
    assert migration._read_persisted_pair_token_pepper() == 'stored-pepper'
    assert seen['flags'] & os.O_NOFOLLOW


def test_missing_file_returns_empty(monkeypatch, migration, tmp_path):
    _root(monkeypatch, migration, tmp_path)
    assert migration._read_persisted_pair_token_pepper() == ''


def test_symlink_is_not_followed(monkeypatch, migration, tmp_path):
    instance = _root(monkeypatch, migration, tmp_path)
    secret = tmp_path / 'secret.txt'
    secret.write_text('followed-secret')
    os.chmod(secret, 0o600)
    (instance / 'pair_token_pepper.txt').symlink_to(secret)
    assert migration._read_persisted_pair_token_pepper() == ''


def test_nofollow_blocks_symlink_when_lstat_is_bypassed(monkeypatch, migration, tmp_path):
    """lstat 被绕过时，O_NOFOLLOW 仍然不能读到链接目标。"""
    instance = _root(monkeypatch, migration, tmp_path)
    secret = tmp_path / 'secret.txt'
    secret.write_text('followed-secret')
    os.chmod(secret, 0o600)
    (instance / 'pair_token_pepper.txt').symlink_to(secret)
    regular = migration.os.lstat(secret)
    monkeypatch.setattr(migration.os, 'lstat', lambda _path: regular)
    assert migration._read_persisted_pair_token_pepper() == ''


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o604, 0o620, 0o606, 0o666])
def test_group_or_other_read_or_write_is_rejected(monkeypatch, migration, tmp_path, mode):
    instance = _root(monkeypatch, migration, tmp_path)
    _write_pepper(instance, 'loose-pepper', mode)
    assert stat.S_IMODE(os.lstat(instance / 'pair_token_pepper.txt').st_mode) == mode
    assert migration._read_persisted_pair_token_pepper() == ''


def test_loose_mode_stays_rejected_in_testing(monkeypatch, migration, tmp_path):
    instance = _root(monkeypatch, migration, tmp_path)
    _write_pepper(instance, 'loose-pepper', 0o644)
    monkeypatch.setenv('TESTING', '1')
    assert migration._read_persisted_pair_token_pepper() == ''


def test_directory_is_rejected(monkeypatch, migration, tmp_path):
    instance = _root(monkeypatch, migration, tmp_path)
    (instance / 'pair_token_pepper.txt').mkdir()
    assert migration._read_persisted_pair_token_pepper() == ''


def test_foreign_owner_rejected_outside_tests(monkeypatch, migration, tmp_path):
    instance = _root(monkeypatch, migration, tmp_path)
    path = _write_pepper(instance, 'owned-pepper')
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.delenv('TESTING', raising=False)
    monkeypatch.setattr(migration.os, 'getuid', lambda: os.stat(path).st_uid + 1)
    assert migration._read_persisted_pair_token_pepper() == ''


def test_foreign_owner_allowed_when_testing_env(monkeypatch, migration, tmp_path):
    instance = _root(monkeypatch, migration, tmp_path)
    path = _write_pepper(instance, 'owned-pepper')
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.setenv('TESTING', 'true')
    monkeypatch.setattr(migration.os, 'getuid', lambda: os.stat(path).st_uid + 1)
    assert migration._read_persisted_pair_token_pepper() == 'owned-pepper'


def test_foreign_owner_allowed_under_pytest(monkeypatch, migration, tmp_path):
    instance = _root(monkeypatch, migration, tmp_path)
    path = _write_pepper(instance, 'owned-pepper')
    monkeypatch.delenv('TESTING', raising=False)
    assert os.getenv('PYTEST_CURRENT_TEST')
    monkeypatch.setattr(migration.os, 'getuid', lambda: os.stat(path).st_uid + 1)
    assert migration._read_persisted_pair_token_pepper() == 'owned-pepper'


def test_rejected_symlink_uses_existing_plaintext_runtime_error(monkeypatch, migration, tmp_path):
    instance = _root(monkeypatch, migration, tmp_path)
    secret = tmp_path / 'secret.txt'
    secret.write_text('followed-secret')
    os.chmod(secret, 0o600)
    (instance / 'pair_token_pepper.txt').symlink_to(secret)
    _clear_pepper_env(monkeypatch)

    engine = sa.create_engine(f'sqlite:///{tmp_path / "pairs.db"}')
    with engine.begin() as conn:
        conn.execute(sa.text(
            'CREATE TABLE pairs (id INTEGER PRIMARY KEY, short_code VARCHAR(12), '
            'short_code_hash VARCHAR(64))'
        ))
        conn.execute(sa.text("INSERT INTO pairs (short_code) VALUES ('abc123')"))
    pepper = migration._migration_pair_token_pepper()
    assert pepper == ''
    with engine.connect() as conn:
        with pytest.raises(RuntimeError, match='PAIR_TOKEN_PEPPER 未设置，无法安全擦除明文短码'):
            migration._require_safe_plaintext_clear(conn, pepper)
    engine.dispose()
