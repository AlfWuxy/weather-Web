# -*- coding: utf-8 -*-
"""后台 pipeline 入口契约测试。"""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
DIRECT_PIPELINES = (
    'services/pipelines/analyze_surnames.py',
    'services/pipelines/dispatch_alerts.py',
    'services/pipelines/import_data.py',
    'services/pipelines/precompute_community_risk.py',
    'services/pipelines/sync_weather_cache.py',
    'services/pipelines/sync_weather_data.py',
)
PRODUCTION_MODULES = (
    'services.pipelines.dispatch_alerts',
    'services.pipelines.sync_weather_cache',
    'services.pipelines.sync_weather_data',
)


def _subprocess_env(tmp_path):
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.update({
        'DATABASE_URI': f"sqlite:///{tmp_path / 'entrypoint.db'}",
        'SECRET_KEY': 'pipeline-entrypoint-test-key',
        'DEBUG': 'true',
        'DEMO_MODE': '1',
        'QWEATHER_KEY': '',
        'AMAP_KEY': '',
        'SILICONFLOW_API_KEY': '',
        'RATE_LIMIT_STORAGE_URI': 'memory://',
        'REDIS_URL': '',
    })
    return env


@pytest.mark.parametrize('relative_path', DIRECT_PIPELINES)
def test_pipeline_file_imports_when_repo_is_not_cwd(tmp_path, relative_path):
    """旧 unit 直接运行深层脚本时，也必须能导入项目包。"""
    script_path = ROOT_DIR / relative_path
    result = subprocess.run(
        [
            sys.executable,
            '-E',
            '-c',
            (
                "import runpy, sys; "
                "runpy.run_path(sys.argv[1], run_name='pipeline_entrypoint_contract')"
            ),
            str(script_path),
        ],
        cwd=tmp_path,
        env=_subprocess_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "No module named 'core'" not in result.stderr


@pytest.mark.parametrize('module_name', PRODUCTION_MODULES)
def test_production_pipeline_modules_expose_help(tmp_path, module_name):
    """标准模块入口必须完成导入并安全返回帮助。"""
    result = subprocess.run(
        [sys.executable, '-m', module_name, '--help'],
        cwd=ROOT_DIR,
        env=_subprocess_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout.lower()


@pytest.mark.parametrize(
    ('script_name', 'arguments', 'expected_arguments'),
    (
        (
            'dispatch_alerts.sh',
            ('--dedupe-hours', '9'),
            ('-m', 'services.pipelines.dispatch_alerts', '--dedupe-hours', '9'),
        ),
        (
            'weather_cache_sync.sh',
            ('--no-daily',),
            ('-m', 'services.pipelines.sync_weather_cache', '--no-daily'),
        ),
        (
            'weather_sync.sh',
            ('2026-07-11',),
            (
                '-m',
                'services.pipelines.sync_weather_data',
                '--daily',
                '--action-daily',
                '--date',
                '2026-07-11',
            ),
        ),
    ),
)
def test_shell_wrapper_uses_repo_root_and_module_entrypoint(
    tmp_path,
    script_name,
    arguments,
    expected_arguments,
):
    """wrapper 从任意目录启动时，固定切到仓库根并使用 python -m。"""
    fake_python = tmp_path / 'fake-python'
    fake_python.write_text(
        '#!/bin/sh\n'
        'printf "cwd=%s\\n" "$PWD"\n'
        'for arg in "$@"; do printf "arg=%s\\n" "$arg"; done\n',
        encoding='utf-8',
    )
    fake_python.chmod(0o755)

    env = _subprocess_env(tmp_path)
    env['VENV_PY'] = str(fake_python)
    result = subprocess.run(
        ['bash', str(ROOT_DIR / 'scripts' / script_name), *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f'cwd={ROOT_DIR}' in result.stdout
    actual_arguments = tuple(
        line.removeprefix('arg=')
        for line in result.stdout.splitlines()
        if line.startswith('arg=')
    )
    assert actual_arguments == expected_arguments
