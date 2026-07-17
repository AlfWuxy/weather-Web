# -*- coding: utf-8 -*-
"""部署环境变量原子更新工具测试。"""

import stat
from pathlib import Path

import pytest

from scripts.update_env_value import update_env_value


def test_update_env_value_preserves_nonempty_value_in_if_empty_mode(tmp_path):
    env_file = tmp_path / '.env'
    env_file.write_text('QWEATHER_KEY=existing\n', encoding='utf-8')

    changed = update_env_value(env_file, 'QWEATHER_KEY', 'replacement', 'if-empty')

    assert changed is False
    assert env_file.read_text(encoding='utf-8') == 'QWEATHER_KEY=existing\n'


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
