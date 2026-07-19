# -*- coding: utf-8 -*-
"""部署环境变量原子更新工具测试。"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.update_env_value as env_value_updater
from scripts.update_env_value import MAX_ENV_VALUE_BYTES, update_env_value


SCRIPT_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'update_env_value.py'


def test_update_env_value_preserves_nonempty_value_in_if_empty_mode(tmp_path):
    env_file = tmp_path / '.env'
    env_file.write_text('QWEATHER_KEY=existing\n', encoding='utf-8')
    env_file.chmod(0o644)

    changed = update_env_value(env_file, 'QWEATHER_KEY', 'replacement', 'if-empty')

    assert changed is False
    assert env_file.read_text(encoding='utf-8') == 'QWEATHER_KEY=existing\n'
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_update_env_value_locks_permissions_for_always_mode_noop(tmp_path):
    env_file = tmp_path / '.env'
    env_file.write_text('SAFE=value\n', encoding='utf-8')
    env_file.chmod(0o644)

    changed = update_env_value(env_file, 'SAFE', 'value', 'always')

    assert changed is False
    assert env_file.read_text(encoding='utf-8') == 'SAFE=value\n'
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_update_env_value_replaces_duplicates_atomically_and_locks_permissions(tmp_path):
    env_file = tmp_path / '.env'
    env_file.write_text(
        'PUBLIC_BASE_URL=\nOTHER=value\nPUBLIC_BASE_URL=https://old.example\n',
        encoding='utf-8',
    )

    changed = update_env_value(
        env_file,
        'PUBLIC_BASE_URL',
        'https://yilaoweather.org',
        'always',
    )

    content = env_file.read_text(encoding='utf-8')
    assert changed is True
    assert content.count('PUBLIC_BASE_URL=') == 1
    assert content.endswith('PUBLIC_BASE_URL=https://yilaoweather.org\n')
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


@pytest.mark.parametrize('key', ('lowercase', 'BAD-NAME', ''))
def test_update_env_value_rejects_invalid_key_without_mutating_file(tmp_path, key):
    env_file = tmp_path / '.env'
    env_file.write_text('SAFE=value\n', encoding='utf-8')

    with pytest.raises(ValueError):
        update_env_value(env_file, key, 'replacement', 'always')

    assert env_file.read_text(encoding='utf-8') == 'SAFE=value\n'


def test_update_env_value_rejects_multiline_secret(tmp_path):
    env_file = Path(tmp_path) / '.env'
    env_file.write_text('SAFE=value\n', encoding='utf-8')

    with pytest.raises(ValueError):
        update_env_value(env_file, 'SAFE', 'first\nsecond', 'always')

    assert env_file.read_text(encoding='utf-8') == 'SAFE=value\n'


def test_update_env_value_rejects_existing_symlink_without_mutating_target(tmp_path):
    real_env = tmp_path / 'real.env'
    real_env.write_text('SAFE=original\n', encoding='utf-8')
    linked_env = tmp_path / '.env'
    linked_env.symlink_to(real_env)

    with pytest.raises(ValueError, match='普通文件'):
        update_env_value(linked_env, 'SAFE', 'replacement', 'always')

    assert linked_env.is_symlink()
    assert real_env.read_text(encoding='utf-8') == 'SAFE=original\n'


def test_update_env_value_rejects_existing_directory_without_mutation(tmp_path):
    env_directory = tmp_path / '.env'
    env_directory.mkdir()

    with pytest.raises(ValueError, match='普通文件'):
        update_env_value(env_directory, 'SAFE', 'replacement', 'always')

    assert env_directory.is_dir()
    assert list(env_directory.iterdir()) == []


def test_update_env_value_rejects_existing_fifo_without_blocking_or_mutation(tmp_path):
    env_fifo = tmp_path / '.env'
    os.mkfifo(env_fifo)

    with pytest.raises(ValueError, match='普通文件'):
        update_env_value(env_fifo, 'SAFE', 'replacement', 'always')

    assert stat.S_ISFIFO(env_fifo.lstat().st_mode)


def test_update_env_value_rejects_value_over_utf8_byte_limit(tmp_path):
    env_file = tmp_path / '.env'
    env_file.write_text('SAFE=original\n', encoding='utf-8')
    oversized_value = '密' * (MAX_ENV_VALUE_BYTES // len('密'.encode('utf-8')) + 1)

    with pytest.raises(ValueError, match='过长'):
        update_env_value(env_file, 'SAFE', oversized_value, 'always')

    assert env_file.read_text(encoding='utf-8') == 'SAFE=original\n'


def test_update_env_value_cli_rejects_oversized_stdin_without_echoing_value(tmp_path):
    env_file = tmp_path / '.env'
    env_file.write_text('SAFE=original\n', encoding='utf-8')
    secret_marker = b'APPSECRET_DO_NOT_ECHO_'
    oversized_value = secret_marker + b'x' * (MAX_ENV_VALUE_BYTES + 1)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            '--file',
            str(env_file),
            '--key',
            'SAFE',
            '--mode',
            'always',
        ],
        input=oversized_value,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert secret_marker not in result.stdout
    assert secret_marker not in result.stderr
    assert env_file.read_text(encoding='utf-8') == 'SAFE=original\n'


def test_update_env_value_cli_accepts_appsecret_without_echoing_it(tmp_path):
    env_file = tmp_path / '.env'
    appsecret = b'0123456789abcdef0123456789abcdef'

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            '--file',
            str(env_file),
            '--key',
            'WX_MINIPROGRAM_SECRET',
            '--mode',
            'always',
        ],
        input=appsecret,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == b''
    assert result.stderr == b''
    assert appsecret not in result.stdout
    assert appsecret not in result.stderr
    assert env_file.read_bytes() == b'WX_MINIPROGRAM_SECRET=' + appsecret + b'\n'
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_update_env_value_if_empty_preserves_concurrent_nonempty_value(
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / '.env'
    env_file.write_text('QWEATHER_KEY=\n', encoding='utf-8')
    env_file.chmod(0o600)
    real_fsync = env_value_updater.os.fsync
    concurrent_value = 'QWEATHER_KEY=concurrent-secret\n'
    replaced = False

    def write_concurrent_value_after_temp_sync(file_descriptor):
        nonlocal replaced
        real_fsync(file_descriptor)
        if not replaced:
            replaced = True
            env_file.write_text(concurrent_value, encoding='utf-8')
            env_file.chmod(0o600)

    monkeypatch.setattr(
        env_value_updater.os,
        'fsync',
        write_concurrent_value_after_temp_sync,
    )

    with pytest.raises(ValueError, match='更新期间发生变化'):
        update_env_value(env_file, 'QWEATHER_KEY', 'candidate-secret', 'if-empty')

    assert env_file.read_text(encoding='utf-8') == concurrent_value
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert not any(tmp_path.glob('..env.*'))
