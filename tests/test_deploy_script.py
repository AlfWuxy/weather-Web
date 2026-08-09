# -*- coding: utf-8 -*-
"""部署脚本回归测试。"""

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY_INSTALLER = ROOT / "scripts" / "install_release_dependencies.sh"
MODEL_ARTIFACT_HELPER = ROOT / "scripts" / "model_artifact.py"


def _load_deploy_script():
    script_path = ROOT / "scripts" / "deploy.sh"
    return script_path.read_text(encoding="utf-8")


def _load_precompute_script():
    script_path = ROOT / "scripts" / "community_risk_precompute.sh"
    return script_path.read_text(encoding="utf-8")


def _load_activate_script():
    script_path = ROOT / "scripts" / "activate_release.sh"
    return script_path.read_text(encoding="utf-8")


def _load_dependency_helper_namespace():
    """只加载依赖安装器内嵌 Python 的声明，便于直接验证树边界函数。"""
    content = DEPENDENCY_INSTALLER.read_text(encoding='utf-8')
    source = content.split("    python3 - \"$@\" <<'PY'\n", 1)[1]
    source = source.split('\nPY\n}', 1)[0]
    parsed = ast.parse(source)
    declarations = tuple(
        node
        for node in parsed.body
        if isinstance(
            node,
            (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef),
        )
    )
    namespace = {}
    exec(
        compile(
            ast.Module(body=list(declarations), type_ignores=[]),
            str(DEPENDENCY_INSTALLER),
            'exec',
        ),
        namespace,
    )
    return namespace


def _write_executable(path, content):
    path.write_text(content, encoding='utf-8')
    path.chmod(0o755)


def _rematerialize_fixture_file(candidate):
    """把平台生成的测试文件收敛为独立 inode 与安全权限。"""
    file_info = candidate.lstat()
    assert stat.S_ISREG(file_info.st_mode)
    descriptor, detached_name = tempfile.mkstemp(
        prefix=f'.{candidate.name}.detached-',
        dir=candidate.parent,
    )
    os.close(descriptor)
    detached = Path(detached_name)
    try:
        shutil.copyfile(candidate, detached, follow_symlinks=False)
        detached.chmod(stat.S_IMODE(file_info.st_mode) & ~0o022)
        os.replace(detached, candidate)
    finally:
        detached.unlink(missing_ok=True)
    normalized = candidate.lstat()
    assert stat.S_ISREG(normalized.st_mode)
    assert normalized.st_nlink == 1
    assert not normalized.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _normalize_fixture_tree(root):
    """只归一化测试 venv 中不满足生产只读契约的普通文件。"""
    for candidate in root.rglob('*'):
        file_info = candidate.lstat()
        if not stat.S_ISREG(file_info.st_mode):
            continue
        if (
            file_info.st_nlink != 1
            or file_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            _rematerialize_fixture_file(candidate)


def _create_reusable_release_base(base, trusted_python_entry):
    release_root = base / 'deploy'
    old_release = release_root / 'releases' / 'old'
    new_release = release_root / 'releases' / 'new'
    old_app = old_release / 'app'
    old_metadata = old_release / 'private-metadata'
    new_app = new_release / 'app'
    for directory in (old_app, old_metadata, new_app):
        directory.mkdir(parents=True, mode=0o700)

    old_venv = old_release / 'venv'
    subprocess.run(
        [sys.executable, '-m', 'venv', str(old_venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    # GitHub runner 可能让多个激活脚本复用 inode 或继承可写权限。
    _normalize_fixture_tree(old_venv)
    python_minor = f'{sys.version_info.major}.{sys.version_info.minor}'
    assert trusted_python_entry.exists()
    source_bin = old_venv / 'bin'
    for python_entry in source_bin.glob('python*'):
        python_entry.unlink()
    (source_bin / 'python').symlink_to('python3')
    (source_bin / 'python3').symlink_to(trusted_python_entry)
    (source_bin / f'python{python_minor}').symlink_to('python3')
    old_python = old_venv / 'bin' / 'python'
    site_packages = Path(
        subprocess.run(
            [
                str(old_python),
                '-c',
                'import sysconfig; print(sysconfig.get_paths()["purelib"])',
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    gunicorn_module = site_packages / 'gunicorn'
    gunicorn_module.mkdir()
    (gunicorn_module / '__init__.py').write_text(
        '__version__ = "0.0.1"\n',
        encoding='utf-8',
    )
    (gunicorn_module / '__main__.py').write_text(
        'raise SystemExit(0)\n',
        encoding='utf-8',
    )
    gunicorn_metadata = site_packages / 'gunicorn-0.0.1.dist-info'
    gunicorn_metadata.mkdir()
    (gunicorn_metadata / 'METADATA').write_text(
        'Metadata-Version: 2.1\nName: gunicorn\nVersion: 0.0.1\n',
        encoding='utf-8',
    )

    inspect_text = subprocess.run(
        [str(old_python), '-m', 'pip', 'inspect', '--local'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    installed = json.loads(inspect_text)['installed']
    lock_text = ''.join(
        f'{item["metadata"]["name"]}=={item["metadata"]["version"]}\n'
        for item in sorted(
            installed,
            key=lambda value: value['metadata']['name'].lower(),
        )
    )
    lock_sha = hashlib.sha256(lock_text.encode()).hexdigest()
    (old_app / 'requirements.lock').write_text(lock_text, encoding='utf-8')
    (new_app / 'requirements.lock').write_text(lock_text, encoding='utf-8')
    (old_metadata / 'requirements-lock.sha256').write_text(
        f'{lock_sha}\n',
        encoding='utf-8',
    )
    version_result = subprocess.run(
        [str(old_python), '--version'],
        check=True,
        capture_output=True,
        text=True,
    )
    python_version = (version_result.stdout or version_result.stderr).strip()
    (old_metadata / 'python-version.txt').write_text(
        f'{python_version}\n',
        encoding='utf-8',
    )
    (old_metadata / 'pip-inspect.json').write_text(
        inspect_text,
        encoding='utf-8',
    )
    for receipt in old_metadata.iterdir():
        # 线上 2026-07-20 legacy release 的依赖收据为 root:case-weather 0640。
        receipt.chmod(0o640)

    current = release_root / 'current'
    current.symlink_to(old_release)
    return {
        'release_root': release_root,
        'old_release': old_release,
        'new_release': new_release,
        'current': current,
        'lock_sha': lock_sha,
        'python_minor': python_minor,
        'trusted_python_entry': trusted_python_entry,
    }


@pytest.fixture(scope='module')
def reusable_release_base(tmp_path_factory):
    helper = _load_dependency_helper_namespace()
    trusted_python_entry = Path(sys.executable).resolve().parent / 'python3'
    trusted_root = None
    try:
        try:
            helper['validate_system_python_chain'](
                trusted_python_entry,
                os.geteuid(),
            )
        except (OSError, helper['ContractError']):
            # GitHub runner 的工具缓存位于可写 /opt；Linux CI 使用受控副本。
            trusted_root = Path(
                tempfile.mkdtemp(
                    prefix='case-weather-test-python-',
                    dir=Path.home(),
                )
            )
            trusted_runtime = trusted_root / 'runtime'
            subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'venv',
                    '--copies',
                    str(trusted_runtime),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            trusted_python_entry = trusted_runtime / 'bin' / 'python3'
            helper['validate_system_python_chain'](
                trusted_python_entry,
                os.geteuid(),
            )
        yield _create_reusable_release_base(
            tmp_path_factory.mktemp('reusable-release-base'),
            trusted_python_entry,
        )
    finally:
        if trusted_root is not None:
            shutil.rmtree(trusted_root)


def _copy_reusable_release(reusable_release_base, tmp_path):
    copied_root = tmp_path / 'fixture'
    shutil.copytree(
        reusable_release_base['release_root'],
        copied_root,
        symlinks=True,
    )
    current = copied_root / 'current'
    current.unlink()
    old_release = copied_root / 'releases' / 'old'
    current.symlink_to(old_release)
    return {
        'release_root': copied_root,
        'old_release': old_release,
        'new_release': copied_root / 'releases' / 'new',
        'current': current,
        'lock_sha': reusable_release_base['lock_sha'],
        'python_minor': reusable_release_base['python_minor'],
        'trusted_python_entry': reusable_release_base[
            'trusted_python_entry'
        ],
    }


def test_normalize_fixture_tree_preserves_content_and_hardens_mode(tmp_path):
    source = tmp_path / 'source.txt'
    candidate = tmp_path / 'candidate.txt'
    writable = tmp_path / 'writable.txt'
    source.write_text('fixture\n', encoding='utf-8')
    source.chmod(0o660)
    os.link(source, candidate)
    writable.write_text('writable\n', encoding='utf-8')
    writable.chmod(0o666)

    _normalize_fixture_tree(tmp_path)

    assert candidate.read_text(encoding='utf-8') == 'fixture\n'
    assert source.read_text(encoding='utf-8') == 'fixture\n'
    assert writable.read_text(encoding='utf-8') == 'writable\n'
    assert candidate.stat().st_nlink == 1
    assert source.stat().st_nlink == 1
    assert stat.S_IMODE(candidate.stat().st_mode) == 0o640
    assert stat.S_IMODE(source.stat().st_mode) == 0o640
    assert stat.S_IMODE(writable.stat().st_mode) == 0o644


def _run_reuse_installer(fixture):
    new_release = fixture['new_release']
    environment = os.environ.copy()
    # helper 必须由测试运行时的受信 Python 启动，不能先执行 source venv。
    trusted_python_bin = fixture['trusted_python_entry'].parent
    environment['PATH'] = f'{trusted_python_bin}:{environment["PATH"]}'
    return subprocess.run(
        [
            'bash',
            str(DEPENDENCY_INSTALLER),
            str(new_release / 'app'),
            str(new_release / 'venv'),
            str(new_release / 'private-metadata'),
            fixture['lock_sha'],
            'reuse-current',
            str(fixture['current']),
            fixture['python_minor'],
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def _create_clean_git_source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'README.md').write_text('release fixture\n', encoding='utf-8')
    model_source = tmp_path / 'model-artifacts'
    model_source.mkdir(mode=0o700)
    model_payloads = {
        'disease_predictor.pkl': b'test-predictor\n',
        'scaler.pkl': b'test-scaler\n',
        'label_encoder.pkl': b'test-labels\n',
    }
    model_specs = {}
    for filename, payload in model_payloads.items():
        artifact = model_source / filename
        artifact.write_bytes(payload)
        artifact.chmod(0o600)
        model_specs[filename] = {
            'sha256': hashlib.sha256(payload).hexdigest(),
            'size_bytes': len(payload),
        }
    verifier = source / 'scripts' / 'verify_github_ci.py'
    verifier.parent.mkdir()
    verifier.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

output = Path(sys.argv[sys.argv.index('--output') + 1])
output.write_text('{}\\n', encoding='utf-8')
os.chmod(output, 0o600)
        """,
        encoding='utf-8',
    )
    model_helper = source / 'scripts' / 'model_artifact.py'
    model_helper.write_text(
        MODEL_ARTIFACT_HELPER.read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    model_helper.chmod(0o755)
    model_manifest = source / 'models' / 'feature_config.json'
    model_manifest.parent.mkdir()
    model_manifest.write_text(
        json.dumps(
            {
                'runtime_artifacts': {
                    'expected_sklearn_version': '1.7.2',
                    'files': model_specs,
                },
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(source)], check=True)
    subprocess.run(
        ['git', '-C', str(source), 'add', '.'],
        check=True,
    )
    subprocess.run(
        [
            'git',
            '-C',
            str(source),
            '-c',
            'user.name=Deploy Test',
            '-c',
            'user.email=deploy-test@example.invalid',
            'commit',
            '-q',
            '-m',
            'test release',
        ],
        check=True,
    )
    return source


def _extract_shell_function(content, name):
    start = content.index(f'{name}() {{')
    end = content.index('\n}\n', start) + len('\n}\n')
    return content[start:end]


def _load_qweather_preactivation_manager_source():
    content = _load_deploy_script()
    function_start = content.index('qweather_preactivation_manager_source() {')
    source_start = content.index("    command cat <<'PY'\n", function_start)
    source_start += len("    command cat <<'PY'\n")
    source_end = content.index('\nPY\n}', source_start)
    return content[source_start:source_end]


def _load_embedded_python_source(function_name):
    content = _load_deploy_script()
    function_start = content.index(f'{function_name}() {{')
    source_start = content.index("    command cat <<'PY'\n", function_start)
    source_start += len("    command cat <<'PY'\n")
    source_end = content.index('\nPY\n}', source_start)
    return content[source_start:source_end]


def _create_qweather_manager_state(tmp_path, release_id='release-20260719'):
    base = (tmp_path / release_id).resolve()
    project = base / 'project'
    release_root = base / 'project-deploy'
    project.mkdir(parents=True, mode=0o700)
    release_root.mkdir(mode=0o700)
    backups = project / 'backups'
    backups.mkdir(mode=0o700)
    activation_root = backups / 'deploy-transactions'
    activation_root.mkdir(mode=0o700)
    private_dir = project / 'private'
    pending_path = private_dir / f'.qweather-jwt.pending-{release_id}'
    final_path = private_dir / 'qweather-ed25519.pem'
    preactivation_root = backups / 'qweather-preactivation'
    manager_path = base / 'qweather_preactivation_manager.py'
    manager_path.write_text(
        _load_qweather_preactivation_manager_source(),
        encoding='utf-8',
    )
    manager_path.chmod(0o700)
    return {
        'base': base,
        'project': project,
        'release_root': release_root,
        'release_id': release_id,
        'private_dir': private_dir,
        'pending_path': pending_path,
        'final_path': final_path,
        'preactivation_root': preactivation_root,
        'activation_root': activation_root,
        'manager_path': manager_path,
    }


def _run_qweather_manager(
    state,
    action,
    payload=b'',
    *,
    expected_payload=None,
):
    identity = str(os.getuid())
    if expected_payload is None:
        expected_payload = payload if action == 'provision' else b'x'
    arguments = [
        sys.executable,
        str(state['manager_path']),
        action,
        str(state['project']),
        str(state['release_root']),
        state['release_id'],
        str(state['private_dir']),
        str(state['pending_path']),
        str(state['final_path']),
        str(state['preactivation_root']),
        str(state['activation_root']),
        identity,
        str(os.getgid()),
        str(os.getgid()),
        hashlib.sha256(expected_payload).hexdigest(),
        str(len(expected_payload)),
    ]
    return subprocess.run(
        ['bash', '-c', 'exec 3<&0; exec "$@"', 'bash', *arguments],
        input=payload,
        capture_output=True,
        check=False,
    )


def _load_qweather_manifest(state):
    path = (
        state['preactivation_root']
        / state['release_id']
        / 'manifest.json'
    )
    return json.loads(path.read_text(encoding='utf-8'))


def _write_activation_plan(state, manifest, transaction_name):
    transaction = state['activation_root'] / transaction_name
    transaction.mkdir(mode=0o700)
    plan = {
        'version': 2,
        'release_id': manifest['release_id'],
        'pending_path': manifest['pending_path'],
        'final_path': manifest['final_path'],
        'sha256': manifest['sha256'],
        'pending_device': manifest['pending_device'],
        'pending_inode': manifest['pending_inode'],
        'pending_nlink': manifest['pending_nlink'],
        'pending_size': manifest['pending_size'],
    }
    plan_path = transaction / 'qweather-key-transition.json'
    plan_path.write_text(
        json.dumps(plan, sort_keys=True, separators=(',', ':')) + '\n',
        encoding='utf-8',
    )
    plan_path.chmod(0o600)
    return plan_path


def _run_qweather_private_key_source_snapshotter(source, deploy_temp_dir):
    content = _load_deploy_script()
    validator_source = _extract_shell_function(
        content, 'validate_qweather_jwt_private_key_snapshot'
    )
    snapshotter_source = _extract_shell_function(
        content, 'snapshot_qweather_jwt_private_key_source'
    )
    environment = os.environ.copy()
    environment['QWEATHER_TEST_PRIVATE_KEY_SOURCE'] = str(source)
    environment['QWEATHER_TEST_DEPLOY_TEMP_DIR'] = str(deploy_temp_dir)
    return subprocess.run(
        [
            'bash',
            '-c',
            'set -euo pipefail\n'
            + validator_source
            + snapshotter_source
            + '\nLOCAL_DEPLOY_TEMP_DIR="$QWEATHER_TEST_DEPLOY_TEMP_DIR"\n'
            + 'LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT=""\n'
            + 'snapshot_qweather_jwt_private_key_source '
            + '"$QWEATHER_TEST_PRIVATE_KEY_SOURCE"\n',
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_remote_path_validator(path_value):
    content = _load_deploy_script()
    validator_source = _extract_shell_function(content, 'validate_remote_path')
    environment = os.environ.copy()
    environment['REMOTE_PATH_UNDER_TEST'] = str(path_value)
    return subprocess.run(
        [
            'bash',
            '-c',
            'set -euo pipefail\n'
            + validator_source
            + '\nvalidate_remote_path TEST_PATH "$REMOTE_PATH_UNDER_TEST"\n',
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )


def test_deploy_script_checks_units_with_is_active():
    content = _load_deploy_script()
    activate = _load_activate_script()

    assert 'case-weather.service' in activate
    assert 'case-weather-backup.timer' in activate
    assert 'case-weather-cache-bootstrap.timer' in activate
    assert 'case-weather-cache.timer' in activate
    assert "bootstrap timer 状态应为 enabled" in activate
    assert "bootstrap timer 未保留完整的首轮 30 分钟等待窗口" in activate
    assert 'check_remote_unit_active "case-weather-dispatch.timer"' not in content
    assert 'case-weather-risk-precompute.timer' in activate
    assert "旧 systemd 单元仍存在" in activate
    assert '"$SYSTEMCTL_BIN" is-active --quiet "$unit"' in activate


def test_deploy_script_no_longer_swallows_systemctl_failures():
    content = _load_deploy_script()

    assert 'case-weather && systemctl restart case-weather && systemctl status --no-pager case-weather || true' not in content
    assert 'case-weather-dispatch.timer && systemctl status --no-pager case-weather-dispatch.timer || true' not in content
    assert 'case-weather-risk-precompute.timer && systemctl status --no-pager case-weather-risk-precompute.timer || true' not in content
    assert 'systemctl stop case-weather || true' not in content


def test_deploy_script_pins_duchang_cache_to_free_tier_budget():
    content = _load_deploy_script()

    assert 'WEATHER_SYNC_LOCATIONS=都昌县' in content
    assert 'QWEATHER_CANONICAL_LOCATION=116.20,29.27' in content
    assert 'QWEATHER_MONTHLY_REQUEST_LIMIT=40000' in content
    assert 'QWEATHER_REQUIRE_PERSISTENT_BUDGET=1' in content
    assert (
        'remote_env_update "QWEATHER_REQUIRE_PERSISTENT_BUDGET" "1" "always"'
        in content
    )
    for key, value in (
        ('QWEATHER_CANONICAL_LOCATION', '116.20,29.27'),
        ('QWEATHER_MONTHLY_REQUEST_LIMIT', '40000'),
        ('QWEATHER_BUDGET_FAIL_CLOSED', '1'),
        ('WEATHER_CACHE_TTL_MINUTES', '30'),
        ('FORECAST_CACHE_TTL_MINUTES', '30'),
        ('QWEATHER_WARNING_CACHE_TTL_MINUTES', '30'),
        ('WEATHER_SYNC_LOCATIONS', '都昌县'),
    ):
        assert f'remote_env_update "{key}" "{value}" "always"' in content
    assert '--probe-persistent-budget' in content
    assert '--seed-persistent-budget' in content
    assert 'QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED' in content
    assert 'QWEATHER_CONSOLE_USAGE_MONTH' in content
    assert 'QWEATHER_CONSOLE_USAGE_BASELINE' in content
    assert 'QWEATHER_EXPECTED_PROJECT_ID' in content
    assert 'QWEATHER_EXPECTED_KID' in content
    assert '微信正式发布必须固定使用 QWEATHER_AUTH_MODE=jwt' in content
    assert '私密发布表记录的 QWeather Project ID/KID 与实际部署配置不一致' in content
    assert content.index('--probe-persistent-budget') < content.index(
        'scripts/release_runtime_smoke.py run'
    )
    assert content.index('--seed-persistent-budget') < content.index(
        'bash $RELEASE_APP/scripts/activate_release.sh'
    )
    assert 'OnUnitInactiveSec=30min' in content
    assert 'OnActiveSec=30min' in content
    assert 'OnSuccess=case-weather-dispatch.service case-weather-cache.timer' in content
    assert 'OnFailure=case-weather-cache.timer' in content
    assert "cat > $NEW_RELEASE/systemd/case-weather-dispatch.timer" not in content
    assert 'ExecStart=/bin/bash $CURRENT_LINK/app/scripts/weather_cache_sync.sh' in content


def test_formal_deploy_propagates_full_feature_release_flags():
    content = _load_deploy_script()

    assert 'FEATURE_WXPUSHER|WXPUSHER_APP_TOKEN|FEATURE_HEAT_EXPOSURE_GIS' in content
    assert 'LOCAL_FEATURE_WXPUSHER' in content
    assert 'LOCAL_FEATURE_HEAT_EXPOSURE_GIS' in content
    assert 'FEATURE_WXPUSHER=0' in content
    assert 'FEATURE_HEAT_EXPOSURE_GIS=0' in content
    assert 'remote_env_update "FEATURE_WXPUSHER" "0" "if-empty"' in content
    assert 'remote_env_update "FEATURE_WXPUSHER" "$LOCAL_FEATURE_WXPUSHER" "always"' in content
    assert 'remote_env_update "FEATURE_HEAT_EXPOSURE_GIS" "0" "if-empty"' in content
    assert 'remote_env_update "FEATURE_HEAT_EXPOSURE_GIS" "$LOCAL_FEATURE_HEAT_EXPOSURE_GIS" "always"' in content
    assert '微信全功能正式发布必须启用 FEATURE_HEAT_EXPOSURE_GIS=1' in content
    assert '1.1.1 微信正式发布必须固定 FEATURE_WXPUSHER=0' in content
    assert 'FEATURE_WXPUSHER=0 时必须清空 WXPUSHER_APP_TOKEN' in content
    assert 'LOCAL_WECHAT_FORMAL_RUNTIME=""' in content
    assert 'LOCAL_WEB_PRIVATE_FEATURES_ENABLED=""' in content
    assert 'WECHAT_FORMAL_RUNTIME=0' in content
    assert 'WEB_PRIVATE_FEATURES_ENABLED=0' in content
    assert (
        'remote_env_update "WECHAT_FORMAL_RUNTIME" '
        '"$LOCAL_WECHAT_FORMAL_RUNTIME" "always"'
    ) in content
    assert (
        'remote_env_update "WEB_PRIVATE_FEATURES_ENABLED" '
        '"$LOCAL_WEB_PRIVATE_FEATURES_ENABLED" "always"'
    ) in content
    assert '微信正式发布必须固定 WECHAT_FORMAL_RUNTIME=1。' in content
    assert '双端正式发布必须固定 WEB_PRIVATE_FEATURES_ENABLED=1。' in content
    assert 'DEBUG=true WECHAT_FORMAL_RUNTIME=0' not in content


def test_web_backend_deploy_inherits_runtime_gate_and_binds_activation_expectation():
    content = _load_deploy_script()
    web_mode_start = content.index(
        'if [ "$DEPLOY_MODE" = "web_backend_only" ]; then'
    )
    web_mode_end = content.index('\nfi', web_mode_start)
    web_mode = content[web_mode_start:web_mode_end]
    candidate_update = content.index(
        'remote_env_update "WEB_PRIVATE_FEATURES_ENABLED" "0" "if-empty"'
    )
    explicit_update = content.index(
        'remote_env_update "WEB_PRIVATE_FEATURES_ENABLED" '
        '"$LOCAL_WEB_PRIVATE_FEATURES_ENABLED" "always"'
    )
    activation = content.index('bash $RELEASE_APP/scripts/activate_release.sh')
    activation_command = content[activation - 2000:activation]

    assert 'LOCAL_WEB_PRIVATE_FEATURES_ENABLED=""' in web_mode
    assert candidate_update < explicit_update
    assert (
        'if [ "$DEPLOY_MODE" = "wechat_formal" ] \\\n'
        '    && [ -n "$LOCAL_WEB_PRIVATE_FEATURES_ENABLED" ]; then'
    ) in content
    assert (
        'EXPECTED_WECHAT_FORMAL_RUNTIME='
        '$EXPECTED_WECHAT_FORMAL_RUNTIME'
    ) in activation_command
    assert (
        'EXPECTED_WEB_PRIVATE_FEATURES_ENABLED='
        '$EXPECTED_WEB_PRIVATE_FEATURES_ENABLED'
    ) in activation_command


def test_formal_deploy_loads_and_forces_audit_logs_off():
    content = _load_deploy_script()
    env_loader_start = content.index("load_local_api_keys() {")
    env_loader_end = content.index("\n}\n", env_loader_start)
    env_loader = content[env_loader_start:env_loader_end]
    form_loader_start = content.index("load_wechat_release_form() {")
    form_loader_end = content.index("\n}\n", form_loader_start)
    form_loader = content[form_loader_start:form_loader_end]

    assert 'LOCAL_FEATURE_AUDIT_LOGS=""' in content
    assert 'FEATURE_AUDIT_LOGS' in env_loader
    assert 'FEATURE_AUDIT_LOGS) LOCAL_FEATURE_AUDIT_LOGS="$value" ;;' in env_loader
    assert 'FEATURE_AUDIT_LOGS' in form_loader
    assert 'FEATURE_AUDIT_LOGS) LOCAL_FEATURE_AUDIT_LOGS="$value" ;;' in form_loader
    assert 'FEATURE_AUDIT_LOGS=0' in content
    assert '微信正式发布必须固定 FEATURE_AUDIT_LOGS=0。' in content
    assert (
        'remote_env_update "FEATURE_AUDIT_LOGS" '
        '"$LOCAL_FEATURE_AUDIT_LOGS" "always"'
    ) in content
    assert 'remote_env_update "FEATURE_AUDIT_LOGS" "0" "if-empty"' not in content


def test_formal_deploy_requires_and_forces_structured_privacy_logs_on():
    content = _load_deploy_script()
    assert 'LOCAL_FEATURE_STRUCTURED_LOGS=""' in content
    assert '微信正式发布必须固定 FEATURE_STRUCTURED_LOGS=1。' in content
    assert 'FEATURE_STRUCTURED_LOGS=1' in content
    assert 'remote_env_update "FEATURE_STRUCTURED_LOGS" "1" "always"' in content
    assert 'remote_env_update "SENTRY_DSN" "" "always"' in content
    assert 'remote_env_update "SENTRY_TRACES_SAMPLE_RATE" "0" "always"' in content
    assert 'remote_env_update "SENTRY_SEND_PII" "0" "always"' in content
    assert (
        'remote_env_update "FEATURE_STRUCTURED_LOGS" '
        '"$LOCAL_FEATURE_STRUCTURED_LOGS" "always"'
    ) in content


def test_inherited_formal_runtime_forces_sentry_privacy_before_validation():
    content = _load_deploy_script()
    resolve = content.index("\nresolve_effective_formal_runtime\n")
    inherited_guard = content.index(
        'if [ "$EFFECTIVE_REQUIRE_WECHAT_READY" = "1" ]; then',
        resolve,
    )
    validation = content.index(
        'remote_exec "python3 $RELEASE_APP/scripts/validate_release_env.py '
        '--file $STAGED_ENV_FILE',
        inherited_guard,
    )

    for update in (
        'remote_env_update "SENTRY_DSN" "" "always"',
        'remote_env_update "SENTRY_TRACES_SAMPLE_RATE" "0" "always"',
        'remote_env_update "SENTRY_SEND_PII" "0" "always"',
    ):
        assert content.count(update) == 1
        assert inherited_guard < content.index(update) < validation


def test_effective_formal_runtime_prepares_and_uploads_miniprogram_proof():
    content = _load_deploy_script()
    helper_start = content.index("verify_miniprogram_release_proof() {")
    helper_end = content.index("\n}\n", helper_start)
    helper = content[helper_start:helper_end]
    proof_bundle_start = content.index("verify_github_release_proofs() {")
    proof_bundle_end = content.index("\n}\n", proof_bundle_start)
    proof_bundle = content[proof_bundle_start:proof_bundle_end]
    first_remote = content.index('remote_exec "echo \'连接成功\'"')
    resolve = content.index("\nresolve_effective_formal_runtime\n")
    inherited_guard = content.index(
        'if [ "$EFFECTIVE_REQUIRE_WECHAT_READY" = "1" ]; then',
        resolve,
    )
    validation = content.index(
        'remote_exec "python3 $RELEASE_APP/scripts/validate_release_env.py '
        '--file $STAGED_ENV_FILE',
        inherited_guard,
    )
    upload_guard = content.index(
        'if [ "$EFFECTIVE_REQUIRE_WECHAT_READY" = "1" ]; then',
        validation,
    )
    upload = content.index(
        '"miniprogram-ci-proof.json"',
        upload_guard,
    )

    assert '--workflow .github/workflows/miniprogram.yml' in helper
    assert '--proof-job "小程序可发布提交证明"' in helper
    assert content.count("verify_miniprogram_release_proof") == 2
    assert "verify_miniprogram_release_proof" in proof_bundle
    assert content.index("\nverify_github_release_proofs\n") < first_remote
    assert "verify_miniprogram_release_proof" not in content[
        inherited_guard:validation
    ]
    assert upload_guard < upload
    assert "if [ '$EFFECTIVE_REQUIRE_WECHAT_READY' = 1 ]; then" in content
    assert "if [ '$DEPLOY_MODE' = wechat_formal ]; then" not in content
    assert (
        'if [ "$DEPLOY_MODE" = "wechat_formal" ]; then\n'
        '    upload_private_metadata_receipt'
    ) not in content


def test_formal_deploy_requires_nginx_access_log_off_before_activation():
    content = _load_deploy_script()
    verification = 'python3 $RELEASE_APP/scripts/verify_runtime_log_boundary.py --active-nginx'
    assert content.count(verification) == 2
    first = content.index(verification)
    second = content.index(verification, first + 1)
    assert first < content.index('步骤4: 准备隔离的候选环境配置')
    assert first < content.index('remote_env_update "WX_MINIPROGRAM_SECRET"')
    assert second > content.index('步骤7: 在单个服务器事务中备份、迁移、切换并验活')
    assert second < content.index('bash $RELEASE_APP/scripts/activate_release.sh')


def test_deploy_script_uses_two_stage_failure_safe_cache_timers():
    content = _load_deploy_script()
    service_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache.service << 'EOF'"
    )
    service_end = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache.timer << 'EOF'",
        service_start,
    )
    service_block = content[service_start:service_end]
    recurring_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache.timer << 'EOF'"
    )
    recurring_end = content.index('EOF"', recurring_start)
    recurring_block = content[recurring_start:recurring_end]
    bootstrap_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-cache-bootstrap.timer << 'EOF'"
    )
    bootstrap_end = content.index('EOF"', bootstrap_start)
    bootstrap_block = content[bootstrap_start:bootstrap_end]

    assert 'OnSuccess=case-weather-dispatch.service case-weather-cache.timer' in service_block
    assert 'OnFailure=case-weather-cache.timer' in service_block
    assert 'OnActiveSec=30min' in recurring_block
    assert 'OnUnitInactiveSec=30min' in recurring_block
    assert 'OnUnitActiveSec=' not in recurring_block
    assert '[Install]' in recurring_block
    assert 'WantedBy=timers.target' in recurring_block
    assert "cat > $NEW_RELEASE/systemd/case-weather-cache-bootstrap.service" not in content
    assert 'OnActiveSec=30min' in bootstrap_block
    assert 'RemainAfterElapse=no' in bootstrap_block
    assert 'Unit=case-weather-cache.service' in bootstrap_block
    assert 'OnUnitActiveSec=' not in bootstrap_block
    assert '[Install]' in bootstrap_block
    activate = _load_activate_script()
    assert 'cache-bootstrap-' not in content
    assert 'cache-bootstrap.success' not in activate
    assert 'NextElapseUSecMonotonic' in activate
    assert 'remaining_us' in activate
    assert 'bootstrap timer 未保留完整的首轮 30 分钟等待窗口' in activate
    assert 'case_2dweather_2dcache_2dbootstrap_2etimer' in activate
    assert 'case-weather-cache-bootstrap.service --property=OnSuccess --value' not in activate
    assert 'case-weather-cache.service --property=OnFailure --value' in activate


def test_deploy_script_sets_precompute_python_path():
    content = _load_deploy_script()

    assert 'Environment=VENV_PY=$CURRENT_LINK/venv/bin/python' in content


def test_deploy_script_uses_isolated_release_and_server_transaction():
    content = _load_deploy_script()
    activate = _load_activate_script()
    installer = DEPENDENCY_INSTALLER.read_text(encoding="utf-8")

    assert 'upload_files "$RELEASE_APP"' in content
    assert 'python3 -m venv "$RELEASE_VENV"' in installer
    assert 'bash $RELEASE_APP/scripts/activate_release.sh' in content
    assert 'upload_files "$PROJECT_DIR"' not in content
    assert 'apt-get' not in content
    assert 'enable --now redis-server' not in content
    assert 'systemctl is-active --quiet redis-server' in content
    assert 'systemctl systemd-run systemd-analyze busctl' in content
    assert 'DB_BACKUP="$TRANSACTION_DIR/database-before.db"' in activate
    assert 'ENV_BACKUP="$TRANSACTION_DIR/environment-before.env"' in activate
    assert 'STAGED_ENV_FILE="$NEW_RELEASE/staged.env"' in content
    assert 'trap on_exit EXIT' in activate
    assert 'flock' in activate.lower()


def test_deploy_requires_verified_private_model_artifacts_before_ssh():
    content = _load_deploy_script()
    prepare = content.index("\nprepare_model_artifacts\n")
    connect = content.index('echo "步骤1: 测试服务器连接..."')
    upload = content.index("\nupload_model_artifacts\n")
    dependency_install = content.index('echo "步骤6: 为新版本创建独立虚拟环境..."')

    assert "ML_MODEL_ARTIFACT_DIR" in content
    assert (
        'require_env_value "ML_MODEL_ARTIFACT_DIR" '
        '"$LOCAL_ML_MODEL_ARTIFACT_DIR"'
    ) in content
    assert 'python3 "$helper" snapshot' in content
    assert '--manifest "$manifest"' in content
    assert '--commit "$VERIFIED_COMMIT"' in content
    assert prepare < connect
    assert upload < dependency_install
    assert (
        "for model_name in disease_predictor.pkl scaler.pkl "
        "label_encoder.pkl"
    ) in content
    assert content.count("--exclude 'models/*.pkl'") == 2
    assert content.count("scripts/model_artifact.py verify") == 2
    assert "model-artifacts.json" in content
    assert (
        "ci-proof.json|miniprogram-ci-proof.json|model-artifacts.json"
    ) in content
    runtime_permissions = content.index("chmod -R g+rX,o-rwx")
    receipt_verification = content.index(
        "--receipt $NEW_RELEASE/private-metadata/model-artifacts.json"
    )
    assert runtime_permissions < receipt_verification
    assert "--commit $VERIFIED_COMMIT" in content[receipt_verification:]
    assert "--expected-owner root" in content[receipt_verification:]
    assert "--expected-group $RUNTIME_GROUP" in content[receipt_verification:]
    assert "--expected-file-mode 0640" in content[receipt_verification:]
    assert "--expected-dir-mode 0750" in content[receipt_verification:]
    assert (
        "$NEW_RELEASE/private-metadata/model-artifacts.json"
        in content[content.index("for PRIVATE_RECEIPT in") :]
    )


def test_dependency_install_is_memory_bounded_before_runtime_preflight():
    content = _load_deploy_script()
    start = content.index('echo "步骤6: 为新版本创建独立虚拟环境..."')
    end = content.index(
        'echo "步骤6.1: 在停止生产服务前完成低内存隔离预检..."'
    )
    install_block = content[start:end]
    activation = content.index('bash $RELEASE_APP/scripts/activate_release.sh')

    assert install_block.count(
        'systemd-run --quiet --wait --pipe --collect'
    ) == 2
    reuse_end = install_block.index(
        'elif [ \\"\\$INSTALL_REUSE_STATUS\\" = 75 ]; then'
    )
    reuse_block = install_block[:reuse_end]
    network_block = install_block[reuse_end:]
    assert '组合峰值约 146 MiB' in reuse_block
    assert '--property=MemoryHigh=160M' in reuse_block
    assert '--property=MemoryMax=192M' in reuse_block
    assert '--property=TasksMax=32' in reuse_block
    assert '--property=TimeoutStartSec=10min' in reuse_block
    assert '--property=RuntimeMaxSec=10min' in reuse_block
    assert '--property=PrivateNetwork=yes' in reuse_block
    assert 'PIP_NO_INDEX=1' in reuse_block
    assert 'reuse-current' in reuse_block
    assert '$CURRENT_LINK' in reuse_block
    assert 'INSTALL_REUSE_MEM_AVAILABLE_KIB=' in reuse_block
    assert '262144' in reuse_block
    assert '可用内存不足 256 MiB' in reuse_block
    assert reuse_block.index('262144') < reuse_block.index(
        'systemd-run --quiet --wait --pipe --collect'
    )
    assert '--property=MemoryHigh=192M' in network_block
    assert '--property=MemoryMax=256M' in network_block
    assert '--property=MemorySwapMax=0' in install_block
    assert '--property=TasksMax=64' in network_block
    assert '--property=OOMPolicy=stop' in install_block
    assert '--property=TimeoutStartSec=15min' in network_block
    assert '--property=RuntimeMaxSec=15min' in network_block
    assert '--property=PrivateTmp=yes' in install_block
    assert '--property=PrivateDevices=yes' in install_block
    assert '--property=NoNewPrivileges=yes' in install_block
    assert '--property=PrivateNetwork=yes' not in network_block
    assert 'INSTALL_MEM_TOTAL_KIB=' in install_block
    assert 'INSTALL_MEM_AVAILABLE_KIB=' in install_block
    assert 'INSTALL_AVAILABLE_MIB=' in install_block
    assert 'INSTALL_INODE_USE_PERCENT=' in install_block
    assert '460800' in install_block
    assert '327680' in install_block
    assert install_block.index('reuse-current') < install_block.index('460800')
    assert install_block.index('reuse-current') < install_block.index('327680')
    assert '2048' in install_block
    assert 'PIP_NO_CACHE_DIR=1' in network_block
    assert 'PIP_DISABLE_PIP_VERSION_CHECK=1' in install_block
    assert 'PYTHONDONTWRITEBYTECODE=1' in install_block
    assert (
        'INSTALL_REUSE_UNIT=case-weather-reuse-$RELEASE_ID.service'
        in install_block
    )
    assert (
        'INSTALL_NETWORK_UNIT=case-weather-install-$RELEASE_ID.service'
        in install_block
    )
    assert 'trap cleanup_install EXIT' in install_block
    assert 'rm -rf -- \\"\\$INSTALL_ROOT\\"' in install_block
    assert 'systemctl stop \\"\\$INSTALL_UNIT\\"' in install_block
    assert 'exit \\"\\$INSTALL_CLEANUP_STATUS\\"' in install_block
    assert (
        install_block.index('systemctl stop \\"\\$INSTALL_UNIT\\"')
        < install_block.index('rm -rf -- \\"\\$INSTALL_ROOT\\"')
    )
    assert 'scripts/install_release_dependencies.sh' in install_block
    assert content.index('scripts/install_release_dependencies.sh') < activation
    assert '非回退型失败' in install_block
    assert 'retry' not in install_block.lower()


def test_dependency_installer_rejects_wrong_lock_before_creating_venv(tmp_path):
    release_venv = tmp_path / "venv"
    metadata_dir = tmp_path / "metadata"

    result = subprocess.run(
        [
            "bash",
            str(DEPENDENCY_INSTALLER),
            str(ROOT),
            str(release_venv),
            str(metadata_dir),
            "0" * 64,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 65
    assert "requirements.lock 摘要不匹配" in result.stderr
    assert not release_venv.exists()
    assert not metadata_dir.exists()


def test_dependency_installer_clones_only_verified_current_release(
    reusable_release_base,
    tmp_path,
):
    fixture = _copy_reusable_release(reusable_release_base, tmp_path)

    result = _run_reuse_installer(fixture)

    assert result.returncode == 0, result.stderr
    new_release = fixture['new_release']
    receipt = json.loads(
        (new_release / 'private-metadata' / 'dependency-receipt.json').read_text(
            encoding='utf-8'
        )
    )
    assert receipt['method'] == 'verified-current-clone'
    assert receipt['source_release_id'] == 'old'
    assert receipt['requirements_lock_sha256'] == fixture['lock_sha']
    assert re.fullmatch(
        r'[0-9a-f]{64}',
        receipt['source_pip_inspect_sha256'],
    )
    assert (new_release / 'venv' / 'bin' / 'python').exists()
    assert subprocess.run(
        [
            str(new_release / 'venv' / 'bin' / 'python'),
            '-c',
            'import gunicorn',
        ],
        check=False,
    ).returncode == 0


@pytest.mark.parametrize(
    ('receipt_name', 'replacement'),
    [
        ('requirements-lock.sha256', '0' * 64 + '\n'),
        ('python-version.txt', 'Python 0.0.0\n'),
        ('pip-inspect.json', '{"installed":[]}\n'),
    ],
)
def test_dependency_reuse_rejects_tampered_private_receipt(
    reusable_release_base,
    tmp_path,
    receipt_name,
    replacement,
):
    fixture = _copy_reusable_release(reusable_release_base, tmp_path)
    receipt = (
        fixture['old_release'] / 'private-metadata' / receipt_name
    )
    receipt.write_text(replacement, encoding='utf-8')
    receipt.chmod(0o640)

    result = _run_reuse_installer(fixture)

    assert result.returncode == 75
    assert '不可安全复用' in result.stderr
    assert not (fixture['new_release'] / 'venv').exists()


def test_dependency_reuse_rejects_lock_or_live_package_drift(
    reusable_release_base,
    tmp_path,
):
    fixture = _copy_reusable_release(reusable_release_base, tmp_path)
    source_lock = fixture['old_release'] / 'app' / 'requirements.lock'
    source_lock.write_text(
        source_lock.read_text(encoding='utf-8') + 'unexpected==1.0\n',
        encoding='utf-8',
    )
    lock_result = _run_reuse_installer(fixture)
    assert lock_result.returncode == 75
    assert not (fixture['new_release'] / 'venv').exists()

    fixture = _copy_reusable_release(
        reusable_release_base,
        tmp_path / 'package-drift',
    )
    old_python = fixture['old_release'] / 'venv' / 'bin' / 'python'
    site_packages = Path(
        subprocess.run(
            [
                str(old_python),
                '-c',
                'import sysconfig; print(sysconfig.get_paths()["purelib"])',
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    (site_packages / 'gunicorn-0.0.1.dist-info' / 'METADATA').write_text(
        'Metadata-Version: 2.1\nName: gunicorn\nVersion: 9.9.9\n',
        encoding='utf-8',
    )
    package_result = _run_reuse_installer(fixture)
    assert package_result.returncode == 75
    assert not (fixture['new_release'] / 'venv').exists()


def test_dependency_reuse_rejects_python_major_minor_drift(
    reusable_release_base,
    tmp_path,
):
    fixture = _copy_reusable_release(reusable_release_base, tmp_path)
    fixture['python_minor'] = '0.0'

    result = _run_reuse_installer(fixture)

    assert result.returncode == 75
    assert not (fixture['new_release'] / 'venv').exists()


def test_dependency_reuse_rejects_unsafe_tree_and_copy_race(
    reusable_release_base,
    tmp_path,
):
    fixture = _copy_reusable_release(reusable_release_base, tmp_path)
    outside = tmp_path / 'outside.txt'
    outside.write_text('do-not-copy\n', encoding='utf-8')
    (
        fixture['old_release'] / 'venv' / 'escaped-link'
    ).symlink_to(outside)

    source_result = _run_reuse_installer(fixture)

    assert source_result.returncode == 75
    assert outside.read_text(encoding='utf-8') == 'do-not-copy\n'
    assert not (fixture['new_release'] / 'venv').exists()

    fixture = _copy_reusable_release(
        reusable_release_base,
        tmp_path / 'target-symlink',
    )
    target = fixture['new_release'] / 'venv'
    target.symlink_to(tmp_path)
    target_result = _run_reuse_installer(fixture)
    assert target_result.returncode == 64
    assert target.is_symlink()
    assert '拒绝覆盖' in target_result.stderr

    fixture = _copy_reusable_release(
        reusable_release_base,
        tmp_path / 'group-writable-module',
    )
    site_packages = next(
        (
            fixture['old_release'] / 'venv' / 'lib'
        ).glob('python*/site-packages')
    )
    execution_marker = tmp_path / 'group-writable-executed'
    gunicorn_init = site_packages / 'gunicorn' / '__init__.py'
    gunicorn_init.write_text(
        'from pathlib import Path\n'
        f'Path({str(execution_marker)!r}).write_text("executed")\n'
        '__version__ = "0.0.1"\n',
        encoding='utf-8',
    )
    gunicorn_init.chmod(0o664)

    writable_result = _run_reuse_installer(fixture)

    assert writable_result.returncode == 75
    assert not execution_marker.exists()
    assert not (fixture['new_release'] / 'venv').exists()

    fixture = _copy_reusable_release(
        reusable_release_base,
        tmp_path / 'fifo-entry',
    )
    os.mkfifo(fixture['old_release'] / 'venv' / 'unsafe-fifo', 0o600)

    fifo_result = _run_reuse_installer(fixture)

    assert fifo_result.returncode == 75
    assert not (fixture['new_release'] / 'venv').exists()

    fixture = _copy_reusable_release(
        reusable_release_base,
        tmp_path / 'hardlink-entry',
    )
    hardlink_source = fixture['old_release'] / 'venv' / 'hardlink-source'
    hardlink_target = fixture['old_release'] / 'venv' / 'hardlink-target'
    hardlink_source.write_text('same inode\n', encoding='utf-8')
    os.link(hardlink_source, hardlink_target)

    hardlink_result = _run_reuse_installer(fixture)

    assert hardlink_result.returncode == 75
    assert not (fixture['new_release'] / 'venv').exists()

    fixture = _copy_reusable_release(
        reusable_release_base,
        tmp_path / 'writable-python-parent',
    )
    execution_marker = tmp_path / 'writable-parent-executed'
    writable_parent = tmp_path / 'replaceable-python'
    writable_parent.mkdir(mode=0o777)
    writable_parent.chmod(0o777)
    malicious_python = writable_parent / 'python'
    _write_executable(
        malicious_python,
        '#!/bin/sh\n'
        f'printf executed > {str(execution_marker)!r}\n'
        f'exec {str(Path(sys.executable).resolve())!r} "$@"\n',
    )
    linkback = writable_parent / 'linkback'
    linkback.symlink_to(Path(sys.executable).resolve())
    source_bin = fixture['old_release'] / 'venv' / 'bin'
    for python_entry in source_bin.glob('python*'):
        python_entry.unlink()
        python_entry.symlink_to(linkback)
    racer = subprocess.Popen(
        [
            sys.executable,
            '-c',
            (
                'import os,sys,time\n'
                'linkback,malicious,trusted=sys.argv[1:]\n'
                'deadline=time.monotonic()+0.5\n'
                'counter=0\n'
                'while time.monotonic()<deadline:\n'
                '    target=malicious if counter%2==0 else trusted\n'
                '    temporary=f"{linkback}.swap-{os.getpid()}"\n'
                '    try:\n'
                '        os.symlink(target,temporary)\n'
                '        os.replace(temporary,linkback)\n'
                '    finally:\n'
                '        try: os.unlink(temporary)\n'
                '        except FileNotFoundError: pass\n'
                '    counter+=1\n'
            ),
            str(linkback),
            str(malicious_python),
            str(Path(sys.executable).resolve()),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    writable_parent_result = _run_reuse_installer(fixture)
    racer.wait(timeout=5)

    assert writable_parent_result.returncode == 75
    assert 'Python 标准链接目标异常' in writable_parent_result.stderr
    assert not execution_marker.exists()
    assert not (fixture['new_release'] / 'venv').exists()

    fixture = _copy_reusable_release(
        reusable_release_base,
        tmp_path / 'dotdot-python-chain',
    )
    dotdot_marker = tmp_path / 'dotdot-chain-executed'
    attacker_root = tmp_path / 'dotdot-attacker'
    attacker_root.mkdir()
    attacker_python = attacker_root / 'python'
    _write_executable(
        attacker_python,
        '#!/bin/sh\n'
        f'printf executed > {str(dotdot_marker)!r}\n'
        f'exec {str(Path(sys.executable).resolve())!r} "$@"\n',
    )
    pivot = tmp_path / 'dotdot-pivot'
    pivot.symlink_to(attacker_root)
    source_python3 = fixture['old_release'] / 'venv' / 'bin' / 'python3'
    source_python3.unlink()
    source_python3.symlink_to(
        f'{pivot}/../../{str(Path(sys.executable).resolve()).lstrip("/")}'
    )

    dotdot_result = _run_reuse_installer(fixture)

    assert dotdot_result.returncode == 75
    assert 'Python 标准链接目标异常' in dotdot_result.stderr
    assert not dotdot_marker.exists()
    assert not (fixture['new_release'] / 'venv').exists()

    fixture = _copy_reusable_release(
        reusable_release_base,
        tmp_path / 'copy-race',
    )
    source_venv = fixture['old_release'] / 'venv'
    slow_directory = source_venv / 'race-slow'
    slow_directory.mkdir()
    for index in range(500):
        (slow_directory / f'{index:04d}.txt').write_text(
            'slow-copy\n',
            encoding='utf-8',
        )
    changing_source = source_venv / 'zz-race-source.txt'
    changing_source.write_text('before\n', encoding='utf-8')
    target_venv = fixture['new_release'] / 'venv'
    racer = subprocess.Popen(
        [
            sys.executable,
            '-c',
            (
                'import os,sys,time\n'
                'target,source=sys.argv[1:]\n'
                'deadline=time.monotonic()+10\n'
                'while time.monotonic()<deadline:\n'
                '    if os.path.lexists(target):\n'
                '        with open(source,"a",encoding="utf-8") as handle:\n'
                '            handle.write("changed-during-copy\\n")\n'
                '        raise SystemExit(0)\n'
                'raise SystemExit(3)\n'
            ),
            str(target_venv),
            str(changing_source),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        race_result = _run_reuse_installer(fixture)
        racer.wait(timeout=12)
    finally:
        if racer.poll() is None:
            racer.terminate()
            racer.wait(timeout=5)

    assert racer.returncode == 0
    assert race_result.returncode == 65
    assert '复制前后发生变化' in race_result.stderr
    assert not target_venv.exists()


def test_dependency_installer_keeps_hashes_binary_only_and_no_cache(tmp_path):
    content = DEPENDENCY_INSTALLER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in content
    assert '--no-cache-dir' in content
    assert '--no-compile' in content
    assert '--require-hashes' in content
    assert '--only-binary=:all:' in content
    assert '"pip",' in content
    assert '"inspect",' in content
    assert '"--local",' in content
    assert 'requirements-lock.sha256' in content
    assert 'dependency-receipt.json' in content
    assert 'shutil.copytree' in content
    assert 'symlinks=True' in content
    assert 'gunicorn' in content
    reuse_flow = content.split('if mode == "reuse":', 1)[1].split(
        'elif mode == "finalize":', 1
    )[0]
    assert reuse_flow.index('source_snapshot = validate_venv_tree(') < (
        reuse_flow.index('live_contract(')
    )
    assert 'post_copy_snapshot = validate_venv_tree(' in reuse_flow
    assert 'target_snapshot = validate_venv_tree(' in reuse_flow
    assert 'require_same_source_snapshot(' in reuse_flow
    assert 'require_matching_copy(' in reuse_flow
    assert 'helper_python_entry = Path(sys.executable)' in (
        reuse_flow
    )
    assert 'validate_system_python_chain(helper_python_entry' in reuse_flow
    assert 'before_exec=source_interpreter_guard' in reuse_flow
    source_compare = reuse_flow.index(
        'source_snapshot, pre_copy_snapshot, "代码核验"'
    )
    source_release = reuse_flow.index('del source_snapshot')
    copy_start = reuse_flow.index('copy_started = True')
    post_compare = reuse_flow.index(
        'pre_copy_snapshot, post_copy_snapshot, "复制"'
    )
    post_release = reuse_flow.index('del post_copy_snapshot')
    target_capture = reuse_flow.index(
        'target_snapshot = validate_venv_tree('
    )
    copy_compare = reuse_flow.index(
        'require_matching_copy(pre_copy_snapshot, target_snapshot)'
    )
    pre_copy_release = reuse_flow.index('del pre_copy_snapshot')
    target_live = reuse_flow.index(
        'new_inspect, new_python_version = live_contract('
    )
    assert source_compare < source_release < copy_start
    assert post_compare < post_release < target_capture
    assert copy_compare < pre_copy_release < target_live

    # 生产运行身份是 root；此处以 UID 0 直接验证非 root 所有者会被拒绝。
    owner_tree = tmp_path / 'non-root-owner'
    owner_tree.mkdir()
    helper = _load_dependency_helper_namespace()
    expected_uid = 0 if os.getuid() != 0 else 1
    with pytest.raises(helper['ContractError'], match='非受信所有者'):
        helper['validate_venv_tree'](
            owner_tree,
            expected_uid,
            Path(sys.executable).resolve().parent / 'python3',
            f'{sys.version_info.major}.{sys.version_info.minor}',
        )


def test_production_gunicorn_uses_single_worker_on_low_memory_host():
    content = _load_deploy_script()
    start = content.index(
        'cat > $NEW_RELEASE/systemd/case-weather.service'
    )
    end = content.index(
        'cat > $NEW_RELEASE/systemd/case-weather-backup.service'
    )
    service_block = content[start:end]

    assert (
        'ExecStart=$CURRENT_LINK/venv/bin/python -m gunicorn'
        in service_block
    )
    assert '$CURRENT_LINK/venv/bin/gunicorn' not in service_block
    assert '--workers 1 --bind 127.0.0.1:5000' in service_block
    assert '--workers 3' not in service_block
    assert '--preload' not in service_block


def test_preflight_finishes_before_server_transaction_can_stop_units():
    content = _load_deploy_script()

    preflight = content.index('scripts/release_runtime_smoke.py run')
    activation = content.index('bash $RELEASE_APP/scripts/activate_release.sh')
    assert preflight < activation


def test_remote_preflight_is_low_memory_private_and_has_no_pytest():
    content = _load_deploy_script()
    start = content.index(
        'echo "步骤6.1: 在停止生产服务前完成低内存隔离预检..."'
    )
    end = content.index('echo "步骤6.2: 为新版本生成 systemd 单元模板..."')
    preflight = content[start:end]

    assert '精确提交的 GitHub CI 收据负责' in preflight
    assert 'remote_exec "set -eu\numask 077\n' in preflight
    assert 'PREFLIGHT_ROOT=$NEW_RELEASE/preflight-runtime' in preflight
    assert 'PREFLIGHT_HOME=\\$PREFLIGHT_ROOT/home' in preflight
    assert 'PREFLIGHT_TMP=\\$PREFLIGHT_ROOT/tmp' in preflight
    assert 'PREFLIGHT_PYCACHE=\\$PREFLIGHT_ROOT/pycache' in preflight
    assert 'PREFLIGHT_RECEIPT=\\$PREFLIGHT_ROOT/runtime-smoke.json' in preflight
    assert (
        'install -d -o $RUNTIME_USER -g $RUNTIME_GROUP -m 0700 '
        '\\"\\$PREFLIGHT_ROOT\\" \\"\\$PREFLIGHT_HOME\\" '
        '\\"\\$PREFLIGHT_TMP\\" \\"\\$PREFLIGHT_PYCACHE\\"'
    ) in preflight
    assert 'chown root:$RUNTIME_GROUP $NEW_RELEASE' in preflight
    assert 'chmod 0750 $NEW_RELEASE' in preflight
    assert 'chown -R root:$RUNTIME_GROUP $RELEASE_APP $RELEASE_VENV' in preflight
    assert 'chmod -R g+rX,o-rwx $RELEASE_APP $RELEASE_VENV' in preflight
    assert preflight.count('systemd-run --quiet --wait --collect') == 3
    assert preflight.count('.service\\"') == 3
    assert preflight.count('--property=User=$RUNTIME_USER') == 3
    assert preflight.count('--property=Group=$RUNTIME_GROUP') == 3
    assert preflight.count('--property=MemoryMax=96M') == 3
    assert preflight.count('--property=MemorySwapMax=0') == 3
    assert preflight.count('--property=TasksMax=64') == 3
    assert preflight.count('--property=OOMPolicy=stop') == 3
    assert preflight.count('--property=PrivateNetwork=yes') == 3
    assert preflight.count('--property=NoNewPrivileges=yes') == 3
    assert preflight.count('/usr/bin/env -i') == 3
    assert 'HOME=\\"\\$PREFLIGHT_HOME\\"' in preflight
    assert 'TMPDIR=\\"\\$PREFLIGHT_TMP\\"' in preflight
    assert 'LANG=C.UTF-8 LC_ALL=C.UTF-8' in preflight
    assert 'USER=$RUNTIME_USER LOGNAME=$RUNTIME_USER' in preflight
    assert 'PYTHONPYCACHEPREFIX=\\"\\$PREFLIGHT_PYCACHE\\"' in preflight
    assert 'PIP_NO_INDEX=1' in preflight
    assert '-m compileall -q -j 1' in preflight
    for shell_path in (
        'scripts/deploy.sh',
        'scripts/install_release_dependencies.sh',
        'scripts/activate_release.sh',
        'scripts/server_migrate.sh',
        'scripts/weather_cache_sync.sh',
        'scripts/backup.sh',
    ):
        assert f'/bin/bash -n {shell_path}' in preflight
    assert '$file' not in preflight
    assert 'scripts/release_runtime_smoke.py run' in preflight
    assert 'scripts/release_runtime_smoke.py \\\n    verify-receipt' in preflight
    assert '--expected-python-minor 3.11' in preflight
    assert 'runtime-smoke.json' in preflight
    assert 'pytest' not in preflight
    assert 'trap cleanup_preflight EXIT' in preflight
    assert 'rm -rf -- \\"\\$PREFLIGHT_ROOT\\"' in preflight

    permission_open = preflight.index(
        'chown -R root:$RUNTIME_GROUP $RELEASE_APP $RELEASE_VENV'
    )
    temp_install = preflight.index('install -d -o $RUNTIME_USER')
    runtime_smoke = preflight.index('scripts/release_runtime_smoke.py run')
    assert permission_open < runtime_smoke
    assert temp_install < runtime_smoke
    assert 'su -c' not in preflight
    assert 'runuser' not in preflight


def test_formal_freeze_preflight_runs_before_remote_change_or_rsync():
    content = _load_deploy_script()

    preflight = content.index('python3 "$SCRIPT_DIR/validate_release_env.py"')
    first_remote_call = content.index('remote_exec "echo \'连接成功\'"')
    first_rsync = content.index("rsync -avz")
    upload = content.index('upload_files "$RELEASE_APP"')

    assert '--repo-root "$LOCAL_DIR"' in content
    assert '--snapshot-output "$VERIFIED_WECHAT_FORM_FILE"' in content
    assert '--verified-commit-output "$VERIFIED_COMMIT_FILE"' in content
    assert preflight < first_remote_call
    assert preflight < first_rsync
    assert preflight < upload
    assert '冻结 GIS gzip 体积必须小于 300 KiB' in (
        ROOT / 'scripts' / 'validate_release_env.py'
    ).read_text(encoding='utf-8')


def test_exact_commit_ci_proof_is_verified_before_first_ssh():
    content = _load_deploy_script()

    proof_call = content.index('\nverify_github_release_proofs\n')
    first_remote_call = content.index('remote_exec "echo \'连接成功\'"')
    first_rsync = content.index('rsync -avz')
    verifier = content[
        content.index('verify_github_release_proofs() {'):
        content.index('\nprepare_release_source\n')
    ]
    proof_helpers = content[
        content.index('verify_miniprogram_release_proof() {'):
        content.index('\nprepare_release_source\n')
    ]

    assert proof_call < first_remote_call
    assert proof_call < first_rsync
    assert '--repo AlfWuxy/weather-Web' in verifier
    assert '--workflow .github/workflows/ci.yml' in verifier
    assert '--commit "$VERIFIED_COMMIT"' in verifier
    assert '--branch "$VERIFIED_RELEASE_BRANCH"' in verifier
    assert '--proof-job "可发布提交证明"' in verifier
    assert '--workflow .github/workflows/miniprogram.yml' in proof_helpers
    assert '--proof-job "小程序可发布提交证明"' in proof_helpers
    assert 'verify_miniprogram_release_proof' in verifier
    assert 'if [ "$DEPLOY_MODE" = "wechat_formal" ]; then' not in verifier
    assert 'main|codex/*' in content
    assert 'symbolic-ref --quiet --short HEAD' in content


def test_ci_and_runtime_receipts_are_bound_to_activation_metadata():
    content = _load_deploy_script()

    ci_upload = content.index(
        'upload_private_metadata_receipt "$LOCAL_CI_PROOF_FILE" "ci-proof.json"'
    )
    runtime_receipt = content.index(
        '$NEW_RELEASE/private-metadata/runtime-smoke.json'
    )
    activation = content.index('bash $RELEASE_APP/scripts/activate_release.sh')

    assert ci_upload < runtime_receipt < activation
    assert 'chmod 0600 \\"\\$TARGET\\"' in content
    assert 'chown -R root:root $NEW_RELEASE/private-metadata' in content
    assert 'chmod -R u=rwX,go= $NEW_RELEASE/private-metadata' in content
    assert '$NEW_RELEASE/private-metadata/dependency-receipt.json' in content
    assert "stat -c '%u:%g:%a' \\\"\\$PRIVATE_RECEIPT\\\"" in content
    assert "'0:0:600'" in content
    assert 'ACTIVATION_EXPECTED_RELEASE_COMMIT="$VERIFIED_COMMIT"' in content
    assert 'EXPECTED_RELEASE_BRANCH=$VERIFIED_RELEASE_BRANCH' in content
    assert 'EXPECTED_RELEASE_COMMIT=$ACTIVATION_EXPECTED_RELEASE_COMMIT' in content
    activate = _load_activate_script()
    assert 'DEPENDENCY_RECEIPT_FILE=' in activate
    assert 'source_pip_inspect_sha256' in activate
    assert '候选虚拟环境依赖完整性检查失败' in activate


def test_deploy_keeps_control_directories_root_private_and_asserts_state():
    content = _load_deploy_script()

    assert 'chown root:root $PROJECT_DIR/backups $PROJECT_DIR/backups/daily $PROJECT_DIR/backups/validation $PROJECT_DIR/deployments' in content
    assert '$PROJECT_DIR/backups/daily' in content
    assert '$PROJECT_DIR/backups/validation' in content
    assert 'chmod 0700 $PROJECT_DIR/backups $PROJECT_DIR/backups/daily $PROJECT_DIR/backups/validation $PROJECT_DIR/deployments' in content
    assert "stat -c '%u:%g:%a' $PROJECT_DIR/backups" in content
    assert "stat -c '%u:%g:%a' $PROJECT_DIR/backups/daily" in content
    assert "stat -c '%u:%g:%a' $PROJECT_DIR/backups/validation" in content
    assert "stat -c '%u:%g:%a' $PROJECT_DIR/deployments" in content
    assert "'0:0:700'" in content


def test_formal_deploy_uploads_verified_commit_snapshot_instead_of_live_tree():
    content = _load_deploy_script()

    assert 'RELEASE_SOURCE_DIR="$LOCAL_DIR"' in content
    assert 'freeze_web_backend_commit() {' in content
    assert (
        'status --porcelain=v1 --untracked-files=all --ignore-submodules=none'
        in content
    )
    assert 'IFS= read -r VERIFIED_COMMIT < "$VERIFIED_COMMIT_FILE"' in content
    assert 'git -C "$LOCAL_DIR" archive --format=tar "$VERIFIED_COMMIT"' in content
    assert 'RELEASE_SOURCE_DIR="$LOCAL_RELEASE_EXPORT_DIR"' in content
    assert '$NEW_RELEASE/private-metadata/source-commit.txt' in content
    assert (
        'ACTIVATION_EXPECTED_RELEASE_COMMIT="$VERIFIED_COMMIT"'
        in content
    )
    assert (
        'EXPECTED_RELEASE_COMMIT=$ACTIVATION_EXPECTED_RELEASE_COMMIT'
        in content
    )
    assert content.count('"$RELEASE_SOURCE_DIR/" "$USER@$SERVER:$remote_target/"') == 2
    assert '"$LOCAL_DIR/" "$USER@$SERVER:$remote_target/"' not in content


def test_deploy_archive_target_comes_from_same_validation_ticket():
    content = _load_deploy_script()
    form_loader_start = content.index("load_wechat_release_form() {")
    form_loader_end = content.index("\n}\n", form_loader_start)
    form_loader = content[form_loader_start:form_loader_end]

    assert "WECHAT_TARGET_COMMIT_SHA" not in form_loader
    assert 'local form_file="$1"' in form_loader
    assert 'done < "$form_file"' in form_loader
    assert "WECHAT_RELEASE_FORM_FILE" not in form_loader
    assert 'VERIFIED_COMMIT_FILE="$LOCAL_DEPLOY_TEMP_DIR/verified-commit"' in content
    assert content.index('--verified-commit-output "$VERIFIED_COMMIT_FILE"') < content.index(
        'IFS= read -r VERIFIED_COMMIT < "$VERIFIED_COMMIT_FILE"'
    )


def test_deploy_loader_uses_the_same_validated_form_snapshot():
    content = _load_deploy_script()

    snapshot_assignment = (
        'VERIFIED_WECHAT_FORM_FILE='
        '"$LOCAL_DEPLOY_TEMP_DIR/wechat-release.snapshot"'
    )
    snapshot_validation = '--snapshot-output "$VERIFIED_WECHAT_FORM_FILE"'
    snapshot_load = 'load_wechat_release_form "$VERIFIED_WECHAT_FORM_FILE"'

    assert snapshot_assignment in content
    assert content.index(snapshot_assignment) < content.index(snapshot_validation)
    assert content.index(snapshot_validation) < content.index(snapshot_load)
    assert "load_wechat_release_form\n" not in content


def test_activate_transaction_stops_every_writer_and_commits_last():
    content = _load_activate_script()
    main_flow = content[content.rindex('\nacknowledge_recovery_transaction\n'):]

    for unit in (
        'case-weather.service',
        'case-weather-backup.service',
        'case-weather-cache.service',
        'case-weather-cache-bootstrap.service',
        'case-weather-dispatch.service',
        'case-weather-risk-precompute.service',
        'case-weather-usage-cleanup.service',
        'case-weather-cache.timer',
        'case-weather-backup.timer',
        'case-weather-cache-bootstrap.timer',
        'case-weather-dispatch.timer',
        'case-weather-sync.timer',
        'case-weather-sync.service',
        'case-weather-risk-precompute.timer',
        'case-weather-usage-cleanup.timer',
    ):
        assert unit in content
    assert content.index('start_candidate_release base\n') < content.index(
        'LINK_MUTATED=1'
    )
    assert main_flow.index('verify_root_crontab_retired_before_activation\n') < (
        main_flow.index('preflight_formal_smoke_cycle_lease\n')
    )
    assert main_flow.index('preflight_formal_smoke_cycle_lease\n') < (
        main_flow.index('verify_formal_runtime_log_boundary\n')
    )
    assert main_flow.index('verify_formal_runtime_log_boundary\n') < (
        main_flow.index('write_durable_marker "$STARTED_MARKER" "$NEW_RELEASE"')
    )
    assert main_flow.index('preflight_formal_smoke_cycle_lease\n') < (
        main_flow.index('MUTATION_STARTED=1')
    )
    assert content.index('install_new_units\n') < content.index(
        'prepare_release_timer_states\n'
    )
    assert content.index('LINK_MUTATED=1') < content.index(
        'prepare_release_timer_states\n'
    )
    assert content.index('prepare_release_timer_states\n') < content.index(
        'validate_managed_backup_service\n'
    )
    assert content.index('validate_managed_backup_service\n') < content.index(
        'validate_installed_backup_service\n'
    )
    assert content.index('validate_installed_backup_service\n') < content.index(
        'verify_pre_request_quiescence\n'
    )
    assert content.index('verify_pre_request_quiescence\n') < content.index(
        'run_formal_cache_smoke\n'
    )
    assert content.index('run_formal_cache_smoke\n') < content.index(
        'start_candidate_release weather\n'
    )
    assert content.index('start_candidate_release weather\n') < content.index(
        'arm_qweather_network_gate\n'
    )
    assert content.index('arm_qweather_network_gate\n') < content.index(
        'start_new_release\n'
    )
    assert content.index('validate_managed_backup_service\n') < content.index(
        'start_new_release\n'
    )
    assert content.index('wait_for_health "$HEALTH_URL"') < content.index('COMMITTED=1')
    assert content.index('start_new_release\n') < content.index('COMMITTED=1')
    assert content.index('FORWARD_ONLY=1') < content.index('COMMITTED=1')
    assert content.index('COMMITTED=1') < content.index('start_release_timers\n')
    assert main_flow.index('write_current_release_ledger "$NEW_RELEASE"') < (
        main_flow.index('COMMITTED=1')
    )
    commit_flow = content.split('\nCOMMITTED=1\n', 1)[1]
    assert commit_flow.index('start_release_timers\n') < commit_flow.index(
        'verify_release_state\n'
    )
    assert commit_flow.index('verify_release_state\n') < commit_flow.index(
        'observe_post_commit_stability\n'
    )
    assert commit_flow.index('observe_post_commit_stability\n') < commit_flow.index(
        '"$TRANSACTION_DIR/COMMITTED"'
    )


def test_activation_forward_only_and_recovery_keep_current_release_ledger_exact():
    content = _load_activate_script()
    forward_start = content.index('if [ "$FORWARD_ONLY" -eq 1 ]; then')
    forward_end = content.index(
        'if [ "$MUTATION_STARTED" -eq 0 ]; then',
        forward_start,
    )
    forward_flow = content[forward_start:forward_end]
    acknowledge_flow = content.split(
        'acknowledge_recovery_transaction() {',
        1,
    )[1].split('\n}', 1)[0]
    receipt_flow = content.split(
        'prepare_formal_smoke_receipt() {',
        1,
    )[1].split('\n}', 1)[0]
    started_write = 'write_durable_marker \\' + '\n        "$started_file"'

    assert forward_flow.index('write_current_release_ledger "$NEW_RELEASE"') < (
        forward_flow.index('durably_sync_release_state forward')
    )
    assert 'COMMITTED=1' not in forward_flow
    assert receipt_flow.index('renew_formal_smoke_cycle_lease') < (
        receipt_flow.index('record_forward_only_phase formal-smoke-started')
    )
    assert receipt_flow.index(
        'record_forward_only_phase formal-smoke-started'
    ) < receipt_flow.index(
        'remove_formal_smoke_lease_journal "$FORMAL_SMOKE_LEASE_JOURNAL"'
    )
    assert receipt_flow.index(
        'remove_formal_smoke_lease_journal "$FORMAL_SMOKE_LEASE_JOURNAL"'
    ) < receipt_flow.index(started_write)
    assert acknowledge_flow.index(
        'reconcile_acknowledged_qweather_key_plan'
    ) < acknowledge_flow.index(
        'reconcile_acknowledged_current_release_ledger'
    )
    assert acknowledge_flow.index(
        'reconcile_acknowledged_current_release_ledger'
    ) < acknowledge_flow.index('confirmation=')
    fingerprint_keys = content.split('keys = (', 1)[1].split('\n)', 1)[0]
    assert "'REDIS_URL'" in fingerprint_keys
    assert "'WEATHER_CACHE_REDIS_URL'" in fingerprint_keys


def test_activation_redis_identity_and_lease_journal_precede_all_mutation():
    content = _load_activate_script()
    main_flow = content[content.rindex('\nverify_candidate_base_state\n'):]
    preflight_flow = content.split(
        'preflight_formal_smoke_cycle_lease() {',
        1,
    )[1].split('\n}', 1)[0]
    recovery_flow = content.split(
        'recover_abandoned_formal_smoke_lease_journals() {',
        1,
    )[1].split('\n}', 1)[0]

    assert main_flow.index('verify_candidate_base_state') < main_flow.index(
        'verify_effective_redis_backend_identity'
    )
    assert main_flow.index(
        'verify_effective_redis_backend_identity'
    ) < main_flow.index('recover_abandoned_formal_smoke_lease_journals')
    assert main_flow.index(
        'recover_abandoned_formal_smoke_lease_journals'
    ) < main_flow.index('acknowledge_recovery_transaction')
    assert main_flow.index('recover_abandoned_formal_smoke_lease_journals') < (
        main_flow.index('mkdir -p "$TRANSACTION_DIR"')
    )
    assert main_flow.index('verify_effective_redis_backend_identity') < (
        main_flow.index('MUTATION_STARTED=1')
    )
    assert preflight_flow.index('write_formal_smoke_lease_journal') < (
        preflight_flow.index('reserve_formal_smoke_cycle_lease')
    )
    assert 'transaction_requires_forward_only "$transaction_path"' in recovery_flow
    assert 'run_formal_smoke_cycle_lease_control release "$ENV_FILE"' in (
        recovery_flow
    )
    assert recovery_flow.index(
        'run_formal_smoke_cycle_lease_control release "$ENV_FILE"'
    ) < recovery_flow.index('remove_formal_smoke_lease_journal "$journal_path"')
    journal_writer = content.split(
        'write_formal_smoke_lease_journal() {',
        1,
    )[1].split('\n}', 1)[0]
    assert 'transaction_id=%s' in journal_writer
    assert 'redis_backend_sha256=%s' in journal_writer
    assert 'lease_token=%s' in journal_writer
    assert 'REDIS_URL=%s' not in journal_writer


def test_deploy_has_no_untracked_hard_checks_after_activation_returns():
    content = _load_deploy_script()
    activation = 'bash $RELEASE_APP/scripts/activate_release.sh"'
    tail = content.split(activation, 1)[1]

    assert 'check_remote_unit_active' not in tail
    assert 'remote_exec "' not in tail
    assert '已在原子激活事务内通过' in tail


def test_deploy_script_excludes_local_design_drafts():
    content = _load_deploy_script()

    assert "--exclude '.claude'" in content
    assert "--exclude '.superpowers'" in content
    assert "--exclude '.pytest_cache'" in content
    assert "--exclude 'backups'" in content
    assert "--exclude 'output'" in content
    assert "--exclude 'tmp'" in content
    assert "--exclude 'blueprints/tools 2.py'" in content
    assert content.count("--exclude '.env*'") == 2
    assert content.count("--exclude '.secrets/'") == 2
    assert content.count("--exclude '*.pem'") == 2
    assert content.count("--exclude '*.key'") == 2
    assert content.count("--exclude 'project.private.config.json'") == 2
    assert "--exclude '.env'" not in content
    assert "--exclude '.env.local'" not in content


def test_deploy_script_requires_https_public_base_url():
    content = _load_deploy_script()

    assert 'ALLOW_INSECURE_PUBLIC_BASE_URL' in content
    assert 'PUBLIC_BASE_URL=https://yilaoweather.org' in content
    assert 'remote_env_update "PUBLIC_BASE_URL" "https://yilaoweather.org" "always"' in content
    assert 'remote_env_update "ALLOW_INSECURE_PUBLIC_BASE_URL" "" "always"' in content
    assert 'DEFAULT_PUBLIC_BASE_URL="http://$SERVER:5000"' not in content
    assert 'scripts/validate_release_env.py --file $STAGED_ENV_FILE' in content


def test_deploy_secrets_use_stdin_and_staged_environment():
    content = _load_deploy_script()

    assert 'remote_exec_with_stdin' in content
    assert 'scripts/update_env_value.py --file $STAGED_ENV_FILE' in content
    assert 'sed -i' not in content
    assert 'QWEATHER_KEY=$LOCAL_QWEATHER_KEY' not in content
    assert 'AMAP_KEY=$LOCAL_AMAP_KEY' not in content
    assert 'AMAP_JS_API_KEY=$LOCAL_AMAP_JS_API_KEY' not in content
    assert 'AMAP_WEB_SERVICE_KEY=$LOCAL_AMAP_WEB_SERVICE_KEY' not in content
    assert 'AMAP_SECURITY_JS_CODE=$LOCAL_AMAP_SECURITY_JS_CODE' not in content
    assert 'WXPUSHER_APP_TOKEN=$LOCAL_WXPUSHER_APP_TOKEN' not in content
    assert content.index('upload_files "$RELEASE_APP"') < content.index(
        'remote_env_update "DATABASE_URI"'
    )
    assert 'ln -s $PROJECT_DIR/.env $RELEASE_APP/.env' not in content


def test_deploy_script_syncs_split_amap_configuration_via_stdin():
    content = _load_deploy_script()

    assert 'LOCAL_AMAP_KEY' not in content
    assert 'AMAP_JS_API_KEY=' in content
    assert 'AMAP_WEB_SERVICE_KEY=' in content
    assert 'AMAP_SECURITY_JS_CODE=' in content
    assert 'COOLING_COORDINATE_VERIFICATION_TTL_DAYS=365' in content
    assert 'remote_env_update "AMAP_KEY" "" "always"' not in content
    assert 'migrate_amap_credentials_atomically' in content
    migration = _load_embedded_python_source('amap_atomic_migration_source')
    assert 'module.update_env_values(' in migration
    assert "'AMAP_KEY': ''" in migration
    assert "'AMAP_JS_API_KEY': js_key" in migration
    assert "'AMAP_WEB_SERVICE_KEY': web_service_key" in migration
    assert "'AMAP_SECURITY_JS_CODE': security_code" in migration
    assert (
        'remote_env_update "COOLING_COORDINATE_VERIFICATION_TTL_DAYS" '
        '"$LOCAL_COOLING_COORDINATE_VERIFICATION_TTL_DAYS" "always"'
    ) in content


def test_amap_migration_switches_legacy_and_split_keys_atomically(tmp_path):
    source = _load_embedded_python_source('amap_atomic_migration_source')
    helper = tmp_path / 'amap-migration.py'
    helper.write_text(source, encoding='utf-8')
    env_file = tmp_path / 'staged.env'
    legacy = 'l' * 32
    env_file.write_text(
        f'AMAP_KEY={legacy}\n'
        'AMAP_JS_API_KEY=\n'
        'AMAP_WEB_SERVICE_KEY=\n'
        'AMAP_SECURITY_JS_CODE=\n',
        encoding='utf-8',
    )
    env_file.chmod(0o600)
    payload = '\n'.join(('j' * 32, 'w' * 32, 's' * 32))

    result = subprocess.run(
        [
            sys.executable,
            str(helper),
            str(env_file),
            str(ROOT / 'scripts' / 'update_env_value.py'),
        ],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    values = dict(
        line.split('=', 1)
        for line in env_file.read_text(encoding='utf-8').splitlines()
    )
    assert values == {
        'AMAP_KEY': '',
        'AMAP_JS_API_KEY': 'j' * 32,
        'AMAP_WEB_SERVICE_KEY': 'w' * 32,
        'AMAP_SECURITY_JS_CODE': 's' * 32,
    }


def test_candidate_base_state_capture_freezes_env_and_current_link(tmp_path):
    source = _load_embedded_python_source(
        'candidate_base_state_capture_source'
    )
    helper = tmp_path / 'capture-base-state.py'
    helper.write_text(source, encoding='utf-8')
    active_env = tmp_path / 'active.env'
    active_env.write_text(
        'SECRET_KEY=canary\n'
        'WECHAT_FORMAL_RUNTIME=1\n'
        'WEB_PRIVATE_FEATURES_ENABLED=0\n',
        encoding='utf-8',
    )
    active_env.chmod(0o600)
    releases = tmp_path / 'releases'
    releases.mkdir()
    current_release = releases / 'old'
    current_release.mkdir()
    current_link = tmp_path / 'current'
    current_link.symlink_to(current_release)
    new_release = releases / 'new'
    new_release.mkdir()
    staged_env = new_release / 'staged.env'
    metadata = new_release / 'private-metadata' / 'candidate-base-state.json'

    result = subprocess.run(
        [
            sys.executable,
            str(helper),
            str(active_env),
            str(staged_env),
            str(current_link),
            str(metadata),
            'web_backend_only',
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert staged_env.read_bytes() == active_env.read_bytes()
    assert stat.S_IMODE(staged_env.stat().st_mode) == 0o600
    values = json.loads(metadata.read_text(encoding='utf-8'))
    assert values['active_env_sha256'] == hashlib.sha256(
        active_env.read_bytes()
    ).hexdigest()
    assert values['current_link_state_sha256'] == hashlib.sha256(
        b'link\0' + os.fsencode(os.readlink(current_link))
    ).hexdigest()
    assert values['deployment_intent'] == 'web_backend_only'
    assert re.fullmatch(r'[0-9a-f]{64}', values['qweather_config_sha256'])
    assert values['wechat_formal_runtime'] == '1'
    assert values['web_private_features_enabled'] == '0'
    assert values['version'] == 3


def test_candidate_base_state_legacy_formal_env_defaults_new_web_gate_closed(
    tmp_path,
):
    source = _load_embedded_python_source(
        'candidate_base_state_capture_source'
    )
    helper = tmp_path / 'capture-base-state.py'
    helper.write_text(source, encoding='utf-8')
    active_env = tmp_path / 'active.env'
    active_env.write_text(
        'WECHAT_FORMAL_RUNTIME=1\n',
        encoding='utf-8',
    )
    active_env.chmod(0o600)
    releases = tmp_path / 'releases'
    releases.mkdir()
    current_release = releases / 'old'
    current_release.mkdir()
    current_link = tmp_path / 'current'
    current_link.symlink_to(current_release)
    new_release = releases / 'new'
    new_release.mkdir()
    staged_env = new_release / 'staged.env'
    metadata = new_release / 'private-metadata' / 'candidate-base-state.json'

    result = subprocess.run(
        [
            sys.executable,
            str(helper),
            str(active_env),
            str(staged_env),
            str(current_link),
            str(metadata),
            'web_backend_only',
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    values = json.loads(metadata.read_text(encoding='utf-8'))
    assert values['wechat_formal_runtime'] == '1'
    assert values['web_private_features_enabled'] == '0'
    assert 'WEB_PRIVATE_FEATURES_ENABLED=' not in staged_env.read_text(
        encoding='utf-8'
    )

    backfill = subprocess.run(
        [
            sys.executable,
            str(ROOT / 'scripts' / 'update_env_value.py'),
            '--file',
            str(staged_env),
            '--key',
            'WEB_PRIVATE_FEATURES_ENABLED',
            '--mode',
            'if-empty',
        ],
        input='0',
        text=True,
        capture_output=True,
        check=False,
    )
    assert backfill.returncode == 0, backfill.stderr
    assert staged_env.read_text(encoding='utf-8').endswith(
        'WEB_PRIVATE_FEATURES_ENABLED=0\n'
    )


@pytest.mark.parametrize(
    'active_text',
    (
        'WEB_PRIVATE_FEATURES_ENABLED=0\n',
        'WECHAT_FORMAL_RUNTIME=2\nWEB_PRIVATE_FEATURES_ENABLED=0\n',
        (
            'WECHAT_FORMAL_RUNTIME=1\n'
            'WEB_PRIVATE_FEATURES_ENABLED=0\n'
            'WEB_PRIVATE_FEATURES_ENABLED=1\n'
        ),
        'WECHAT_FORMAL_RUNTIME=1\nWEB_PRIVATE_FEATURES_ENABLED=yes\n',
    ),
)
def test_candidate_base_state_rejects_invalid_or_duplicate_runtime_gates(
    tmp_path,
    active_text,
):
    source = _load_embedded_python_source(
        'candidate_base_state_capture_source'
    )
    helper = tmp_path / 'capture-base-state.py'
    helper.write_text(source, encoding='utf-8')
    active_env = tmp_path / 'active.env'
    active_env.write_text(active_text, encoding='utf-8')
    active_env.chmod(0o600)
    releases = tmp_path / 'releases'
    releases.mkdir()
    current_release = releases / 'old'
    current_release.mkdir()
    current_link = tmp_path / 'current'
    current_link.symlink_to(current_release)
    new_release = releases / 'new'
    new_release.mkdir()
    staged_env = new_release / 'staged.env'
    metadata = new_release / 'private-metadata' / 'candidate-base-state.json'

    result = subprocess.run(
        [
            sys.executable,
            str(helper),
            str(active_env),
            str(staged_env),
            str(current_link),
            str(metadata),
            'web_backend_only',
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert not staged_env.exists()
    assert not metadata.exists()


def test_web_mode_backfills_only_account_link_pepper_for_formal_runtime(
    tmp_path,
):
    source = _load_embedded_python_source(
        'account_link_pepper_backfill_source'
    )
    helper = tmp_path / 'backfill-account-link.py'
    helper.write_text(source, encoding='utf-8')
    staged_env = tmp_path / 'staged.env'
    staged_env.write_text(
        'WECHAT_FORMAL_RUNTIME=1\n'
        'WX_MINIPROGRAM_APPID=wx-canary\n'
        'WX_MINIPROGRAM_SECRET=secret-canary\n'
        'WX_MINIPROGRAM_OPENID_PEPPER=openid-canary\n'
        'WX_MINIPROGRAM_SESSION_SECRET=session-canary\n'
        'ACCOUNT_LINK_CODE_PEPPER=\n',
        encoding='utf-8',
    )
    staged_env.chmod(0o600)

    result = subprocess.run(
        [
            sys.executable,
            str(helper),
            str(staged_env),
            str(ROOT / 'scripts' / 'update_env_value.py'),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    values = dict(
        line.split('=', 1)
        for line in staged_env.read_text(encoding='utf-8').splitlines()
    )
    assert re.fullmatch(r'[0-9a-f]{64}', values['ACCOUNT_LINK_CODE_PEPPER'])
    assert values['WX_MINIPROGRAM_SECRET'] == 'secret-canary'
    first_pepper = values['ACCOUNT_LINK_CODE_PEPPER']
    repeated = subprocess.run(
        [
            sys.executable,
            str(helper),
            str(staged_env),
            str(ROOT / 'scripts' / 'update_env_value.py'),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert repeated.returncode == 0
    assert f'ACCOUNT_LINK_CODE_PEPPER={first_pepper}' in staged_env.read_text(
        encoding='utf-8'
    )

    web_env = tmp_path / 'web.env'
    web_env.write_text(
        'WECHAT_FORMAL_RUNTIME=0\nACCOUNT_LINK_CODE_PEPPER=\n',
        encoding='utf-8',
    )
    web_env.chmod(0o600)
    untouched = subprocess.run(
        [
            sys.executable,
            str(helper),
            str(web_env),
            str(ROOT / 'scripts' / 'update_env_value.py'),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert untouched.returncode == 0
    assert web_env.read_text(encoding='utf-8').endswith(
        'ACCOUNT_LINK_CODE_PEPPER=\n'
    )


def test_qweather_jwt_private_key_source_is_local_only_and_uses_file_stdin():
    content = _load_deploy_script()
    initial_env = content.split('cat > $PROJECT_DIR/.env << EOF', 1)[1].split(
        '\nEOF', 1
    )[0]

    assert 'QWEATHER_JWT_PRIVATE_KEY_SOURCE' in content
    assert 'remote_env_update "QWEATHER_JWT_PRIVATE_KEY_SOURCE"' not in content
    assert 'QWEATHER_JWT_PRIVATE_KEY_SOURCE' not in initial_env
    runner = _extract_shell_function(
        content, 'run_qweather_preactivation_manager'
    )
    assert 'remote_exec_with_file_stdin' in runner
    assert '"$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT"' in runner
    assert 'remote_exec_with_file_stdin "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_SOURCE"' not in content
    assert 'ssh $SSH_OPTS "$USER@$SERVER" "$remote_command" < "$local_file"' in content
    assert 'QWEATHER_JWT_PRIVATE_KEY_PATH 必须位于 DEPLOY_PROJECT_DIR/private/' in content
    assert 'QWEATHER_JWT_PRIVATE_KEY_PATH 必须是 DEPLOY_PROJECT_DIR/private/ 下的直接文件' in content


def test_qweather_jwt_private_key_snapshot_is_private_and_revalidated(tmp_path):
    content = _load_deploy_script()
    validator = _extract_shell_function(
        content, 'validate_qweather_jwt_private_key_snapshot'
    )
    snapshotter = _extract_shell_function(
        content, 'snapshot_qweather_jwt_private_key_source'
    )
    private_key = tmp_path / 'qweather-ed25519.pem'
    subprocess.run(
        ['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(private_key)],
        check=True,
        capture_output=True,
        text=True,
    )
    private_key.chmod(0o600)
    deploy_temp_dir = tmp_path / 'case-weather-deploy.test'
    deploy_temp_dir.mkdir(mode=0o700)

    result = _run_qweather_private_key_source_snapshotter(
        private_key, deploy_temp_dir
    )

    assert result.returncode == 0
    snapshot = deploy_temp_dir / 'qweather-jwt-private'
    assert snapshot.is_file()
    assert not snapshot.is_symlink()
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert snapshot.read_bytes() == private_key.read_bytes()
    assert result.stderr == ''
    assert content.count(
        'LOCAL_DEPLOY_TEMP_DIR="$(mktemp -d '
        '"${TMPDIR:-/tmp}/case-weather-deploy.XXXXXX")"'
    ) == 1
    assert 'chmod 0700 "$LOCAL_DEPLOY_TEMP_DIR"' in content
    assert '本轮部署临时目录权限必须精确为 0700' in content
    assert 'local snapshot="$LOCAL_DEPLOY_TEMP_DIR/qweather-jwt-private"' in snapshotter
    assert snapshotter.count('source_descriptor = os.open(') == 1
    assert 'os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC' in snapshotter
    assert 'os.fstat(source_descriptor)' in snapshotter
    assert 'stat.S_IMODE(before.st_mode) != 0o600' in snapshotter
    assert 'before.st_size <= 0 or before.st_size > MAX_PRIVATE_KEY_BYTES' in snapshotter
    create_snapshot = snapshotter.index(
        'os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC'
    )
    revalidate = snapshotter.index(
        'validate_qweather_jwt_private_key_snapshot "$snapshot"'
    )
    publish_snapshot = snapshotter.index(
        'LOCAL_QWEATHER_JWT_PRIVATE_KEY_SNAPSHOT="$snapshot"'
    )
    assert create_snapshot < revalidate < publish_snapshot
    assert validator.count('openssl pkey -in "$snapshot"') == 2
    assert 'openssl pkey -in "$source"' not in content
    assert result.stdout == ''


def test_qweather_jwt_private_key_is_provisioned_after_candidate_is_ready():
    content = _load_deploy_script()

    immutable_release = content.index(
        'remote_exec "if [ -e $NEW_RELEASE ]; then '
        "echo '发布 ID 已存在，拒绝覆盖不可变版本: $NEW_RELEASE'"
    )
    upload = content.index('upload_files "$RELEASE_APP"')
    staged_env = content.index('\ncapture_remote_candidate_base_state\n')
    final_candidate_update = content.index(
        'remote_env_update "QWEATHER_EXPECTED_KID" '
        '"$LOCAL_QWEATHER_EXPECTED_KID" "always"'
    )
    provision = content.index('\nprovision_qweather_jwt_private_key\n')
    remote_validate = content.index(
        'remote_exec "python3 $RELEASE_APP/scripts/validate_release_env.py '
        '--file $STAGED_ENV_FILE '
        '--require-wechat $EFFECTIVE_REQUIRE_WECHAT_READY '
        '--require-weather-ready $REMOTE_QWEATHER_VALIDATION_PENDING_ARG"'
    )

    assert immutable_release < upload < staged_env
    assert staged_env < final_candidate_update < provision < remote_validate


def test_qweather_jwt_private_key_remote_pending_install_is_fail_closed():
    content = _load_deploy_script()
    manager = _load_qweather_preactivation_manager_source()
    runner = _extract_shell_function(
        content, 'run_qweather_preactivation_manager'
    )
    cleanup = _extract_shell_function(content, 'cleanup_local_deploy_temp')

    assert (
        'REMOTE_QWEATHER_PENDING_KEY_PATH='
        '"$REMOTE_QWEATHER_PRIVATE_DIR/.qweather-jwt.pending-$RELEASE_ID"'
    ) in content
    assert "exec 9>'$RELEASE_ROOT/deploy.lock'" in runner
    assert 'if ! flock -n 9; then' in runner
    assert 'remote_exec_with_file_stdin' in runner
    assert "'pending_device', 'pending_inode', 'pending_nlink', 'pending_size'" in manager
    assert "mode=0o600,\n        nlink=1" in manager
    assert 'os.replace(current, pending_path)' in manager
    assert 'os.replace(source, destination)' in manager
    assert "if activation_adopted(manifest):\n        return 'activation-adopted'" in manager
    assert 'validate_existing_final(payload)' in manager
    assert 'QWeather 私钥目标内容不同，停止发布且不覆盖。' in manager
    assert 'archive_qweather_preactivation_key' in cleanup
    assert 'exit "$original_status"' in cleanup
    assert content.index('\nreconcile_qweather_preactivation_transactions\n') < content.index(
        '发布 ID 已存在，拒绝覆盖不可变版本'
    )


def test_qweather_existing_final_key_must_match_without_preflight_publication():
    manager = _load_qweather_preactivation_manager_source()

    compare_existing = manager.index('validate_existing_final(payload)')
    create_transaction = manager.index(
        'transaction = ensure_transaction(release_id, create=True)'
    )
    publish_pending = manager.index('os.replace(current, pending_path)')

    assert compare_existing < create_transaction < publish_pending
    assert 'uid=owner_uid,\n        gid=runtime_gid,\n        mode=0o640' in manager
    assert 'final_payload != payload' in manager
    assert "echo 'QWeather JWT 私钥已安全复用。'" not in manager


def test_qweather_preactivation_archive_is_durable_idempotent_and_recoverable(
    tmp_path,
):
    state = _create_qweather_manager_state(tmp_path)
    payload = b'synthetic-ed25519-private-key\n'

    provision = _run_qweather_manager(state, 'provision', payload)
    assert provision.returncode == 0, provision.stderr.decode()
    pending = state['pending_path']
    manifest = _load_qweather_manifest(state)
    pending_stat = pending.stat()
    assert pending.read_bytes() == payload
    assert pending_stat.st_mode & 0o777 == 0o600
    assert pending_stat.st_nlink == 1
    assert manifest['sha256'] == hashlib.sha256(payload).hexdigest()
    assert manifest['pending_device'] == pending_stat.st_dev
    assert manifest['pending_inode'] == pending_stat.st_ino

    archive = _run_qweather_manager(state, 'archive')
    assert archive.returncode == 0, archive.stderr.decode()
    recovery = (
        state['preactivation_root']
        / state['release_id']
        / 'qweather-key-recovery'
        / 'pending.pem'
    )
    assert not pending.exists()
    assert recovery.read_bytes() == payload
    assert recovery.stat().st_ino == pending_stat.st_ino

    repeated_archive = _run_qweather_manager(state, 'archive')
    assert repeated_archive.returncode == 0, repeated_archive.stderr.decode()
    assert recovery.stat().st_ino == pending_stat.st_ino

    retry = _run_qweather_manager(state, 'provision', payload)
    assert retry.returncode == 0, retry.stderr.decode()
    assert pending.stat().st_ino == pending_stat.st_ino
    reconcile = _run_qweather_manager(state, 'reconcile-all')
    assert reconcile.returncode == 0, reconcile.stderr.decode()
    assert not pending.exists()
    assert recovery.stat().st_ino == pending_stat.st_ino


def test_qweather_preactivation_recovers_manifestless_sigkill_state(tmp_path):
    state = _create_qweather_manager_state(tmp_path)
    payload = b'synthetic-source-before-manifest\n'
    state['private_dir'].mkdir(mode=0o700)
    state['preactivation_root'].mkdir(mode=0o700)
    transaction = state['preactivation_root'] / state['release_id']
    transaction.mkdir(mode=0o700)
    source = transaction / 'source.pem'
    source.write_bytes(payload)
    source.chmod(0o600)
    source_inode = source.stat().st_ino

    reconcile = _run_qweather_manager(state, 'reconcile-all')
    assert reconcile.returncode == 0, reconcile.stderr.decode()
    unproven = transaction / 'qweather-key-recovery' / 'unproven.pem'
    marker = transaction / 'UNPROVEN_ARCHIVED.json'
    assert not source.exists()
    assert unproven.stat().st_ino == source_inode
    assert marker.stat().st_mode & 0o777 == 0o600

    marker.unlink()
    repair_marker = _run_qweather_manager(state, 'reconcile-all')
    assert repair_marker.returncode == 0, repair_marker.stderr.decode()
    assert marker.stat().st_mode & 0o777 == 0o600

    os.replace(unproven, source)
    retry = _run_qweather_manager(state, 'provision', payload)
    assert retry.returncode == 0, retry.stderr.decode()
    assert state['pending_path'].stat().st_ino == source_inode
    assert _load_qweather_manifest(state)['pending_inode'] == source_inode


def test_qweather_preactivation_recovers_half_written_canonical_manifest(
    tmp_path,
):
    state = _create_qweather_manager_state(tmp_path, 'half-manifest-release')
    payload = b'synthetic-complete-private-key\n'
    state['private_dir'].mkdir(mode=0o700)
    state['preactivation_root'].mkdir(mode=0o700)
    transaction = state['preactivation_root'] / state['release_id']
    transaction.mkdir(mode=0o700)
    source = transaction / 'source.pem'
    source.write_bytes(payload)
    source.chmod(0o600)
    source_stat = source.stat()
    expected_manifest = {
        'version': 1,
        'release_id': state['release_id'],
        'pending_path': str(state['pending_path']),
        'final_path': str(state['final_path']),
        'sha256': hashlib.sha256(payload).hexdigest(),
        'pending_device': source_stat.st_dev,
        'pending_inode': source_stat.st_ino,
        'pending_nlink': 1,
        'pending_size': source_stat.st_size,
    }
    encoded = (
        json.dumps(expected_manifest, sort_keys=True, separators=(',', ':'))
        + '\n'
    ).encode('utf-8')
    manifest = transaction / 'manifest.json'
    manifest.write_bytes(encoded[: len(encoded) // 2])
    manifest.chmod(0o600)
    manifest_inode = manifest.stat().st_ino

    retry = _run_qweather_manager(state, 'provision', payload)

    assert retry.returncode == 0, retry.stderr.decode()
    assert state['pending_path'].read_bytes() == payload
    assert _load_qweather_manifest(state)['sha256'] == hashlib.sha256(
        payload
    ).hexdigest()
    evidence = list(
        (transaction / 'qweather-key-recovery').glob(
            'evidence-partial-manifest-*.bin'
        )
    )
    assert len(evidence) == 1
    assert evidence[0].stat().st_ino == manifest_inode
    assert evidence[0].read_bytes() == encoded[: len(encoded) // 2]


def test_qweather_preactivation_recovers_half_written_source_after_archive(
    tmp_path,
):
    state = _create_qweather_manager_state(tmp_path, 'half-source-release')
    payload = b'synthetic-complete-private-key\n'
    partial = payload[:11]
    state['private_dir'].mkdir(mode=0o700)
    state['preactivation_root'].mkdir(mode=0o700)
    transaction = state['preactivation_root'] / state['release_id']
    transaction.mkdir(mode=0o700)
    source = transaction / 'source.pem'
    source.write_bytes(partial)
    source.chmod(0o600)
    partial_inode = source.stat().st_ino

    archived = _run_qweather_manager(state, 'archive')
    assert archived.returncode == 0, archived.stderr.decode()
    retry = _run_qweather_manager(state, 'provision', payload)

    assert retry.returncode == 0, retry.stderr.decode()
    assert state['pending_path'].read_bytes() == payload
    assert state['pending_path'].stat().st_ino != partial_inode
    recovery = transaction / 'qweather-key-recovery'
    evidence = list(recovery.glob('evidence-partial-source-*.bin'))
    assert len(evidence) == 1
    assert evidence[0].stat().st_ino == partial_inode
    assert evidence[0].read_bytes() == partial
    assert len(list(recovery.glob('evidence-partial-record-*.bin'))) == 1


def test_qweather_preactivation_recovers_atomic_temp_sigkill_windows(tmp_path):
    payload = b'synthetic-complete-private-key\n'

    source_state = _create_qweather_manager_state(
        tmp_path, 'source-temp-sigkill-release'
    )
    source_state['private_dir'].mkdir(mode=0o700)
    source_state['preactivation_root'].mkdir(mode=0o700)
    source_transaction = (
        source_state['preactivation_root'] / source_state['release_id']
    )
    source_transaction.mkdir(mode=0o700)
    source_temp = source_transaction / ('.atomic-source.pem-' + 'a' * 32)
    source_temp.write_bytes(payload[:7])
    source_temp.chmod(0o600)
    source_temp_inode = source_temp.stat().st_ino

    source_retry = _run_qweather_manager(source_state, 'provision', payload)
    assert source_retry.returncode == 0, source_retry.stderr.decode()
    assert source_state['pending_path'].read_bytes() == payload
    source_evidence = list(
        (source_transaction / 'qweather-key-recovery').glob(
            'evidence-temp-source-*.bin'
        )
    )
    assert len(source_evidence) == 1
    assert source_evidence[0].stat().st_ino == source_temp_inode

    manifest_state = _create_qweather_manager_state(
        tmp_path, 'manifest-temp-sigkill-release'
    )
    manifest_state['private_dir'].mkdir(mode=0o700)
    manifest_state['preactivation_root'].mkdir(mode=0o700)
    manifest_transaction = (
        manifest_state['preactivation_root'] / manifest_state['release_id']
    )
    manifest_transaction.mkdir(mode=0o700)
    manifest_source = manifest_transaction / 'source.pem'
    manifest_source.write_bytes(payload)
    manifest_source.chmod(0o600)
    manifest_temp = manifest_transaction / (
        '.atomic-manifest.json-' + 'b' * 32
    )
    manifest_temp.write_bytes(b'{"final_path":')
    manifest_temp.chmod(0o600)
    manifest_temp_inode = manifest_temp.stat().st_ino

    manifest_retry = _run_qweather_manager(
        manifest_state, 'provision', payload
    )
    assert manifest_retry.returncode == 0, manifest_retry.stderr.decode()
    assert manifest_state['pending_path'].read_bytes() == payload
    manifest_evidence = list(
        (manifest_transaction / 'qweather-key-recovery').glob(
            'evidence-temp-manifest-*.bin'
        )
    )
    assert len(manifest_evidence) == 1
    assert manifest_evidence[0].stat().st_ino == manifest_temp_inode


def test_qweather_preactivation_rejects_early_eof_before_remote_mutation(
    tmp_path,
):
    state = _create_qweather_manager_state(tmp_path, 'early-eof-release')
    complete = b'synthetic-complete-private-key\n'
    result = _run_qweather_manager(
        state,
        'provision',
        complete[:9],
        expected_payload=complete,
    )

    assert result.returncode == 64
    assert '私钥传输不完整' in result.stderr.decode()
    assert not state['private_dir'].exists()
    assert not state['preactivation_root'].exists()


def test_qweather_preactivation_half_write_recovery_stays_fail_closed_on_tamper(
    tmp_path,
):
    payload = b'synthetic-complete-private-key\n'
    hardlink_state = _create_qweather_manager_state(
        tmp_path, 'half-source-hardlink-release'
    )
    hardlink_state['private_dir'].mkdir(mode=0o700)
    hardlink_state['preactivation_root'].mkdir(mode=0o700)
    transaction = (
        hardlink_state['preactivation_root'] / hardlink_state['release_id']
    )
    transaction.mkdir(mode=0o700)
    source = transaction / 'source.pem'
    source.write_bytes(payload[:8])
    source.chmod(0o600)
    escaped = hardlink_state['base'] / 'escaped-source.pem'
    os.link(source, escaped)

    hardlink_retry = _run_qweather_manager(
        hardlink_state, 'provision', payload
    )
    assert hardlink_retry.returncode == 64
    assert source.exists()
    assert source.stat().st_nlink == 2
    assert not list(transaction.rglob('evidence-*.bin'))

    manifest_state = _create_qweather_manager_state(
        tmp_path, 'half-manifest-tamper-release'
    )
    manifest_state['private_dir'].mkdir(mode=0o700)
    manifest_state['preactivation_root'].mkdir(mode=0o700)
    manifest_transaction = (
        manifest_state['preactivation_root'] / manifest_state['release_id']
    )
    manifest_transaction.mkdir(mode=0o700)
    manifest_source = manifest_transaction / 'source.pem'
    manifest_source.write_bytes(payload)
    manifest_source.chmod(0o600)
    manifest = manifest_transaction / 'manifest.json'
    manifest.write_bytes(b'root-authored-but-not-a-manifest')
    manifest.chmod(0o600)
    manifest_inode = manifest.stat().st_ino

    tampered_retry = _run_qweather_manager(
        manifest_state, 'provision', payload
    )
    assert tampered_retry.returncode == 64
    assert manifest.stat().st_ino == manifest_inode
    assert not list(manifest_transaction.rglob('evidence-*.bin'))

    symlink_state = _create_qweather_manager_state(
        tmp_path, 'temp-symlink-release'
    )
    symlink_state['private_dir'].mkdir(mode=0o700)
    symlink_state['preactivation_root'].mkdir(mode=0o700)
    symlink_transaction = (
        symlink_state['preactivation_root'] / symlink_state['release_id']
    )
    symlink_transaction.mkdir(mode=0o700)
    outside = symlink_state['base'] / 'outside-secret'
    outside.write_bytes(payload[:5])
    outside.chmod(0o600)
    temp_symlink = symlink_transaction / (
        '.atomic-source.pem-' + 'c' * 32
    )
    temp_symlink.symlink_to(outside)

    symlink_retry = _run_qweather_manager(
        symlink_state, 'provision', payload
    )
    assert symlink_retry.returncode == 64
    assert temp_symlink.is_symlink()
    assert outside.read_bytes() == payload[:5]
    assert not list(symlink_transaction.rglob('evidence-*.bin'))


def test_qweather_preactivation_rejects_tamper_and_payload_mismatch(tmp_path):
    payload = b'synthetic-tamper-check\n'

    hardlink_state = _create_qweather_manager_state(tmp_path, 'hardlink-release')
    assert _run_qweather_manager(
        hardlink_state, 'provision', payload
    ).returncode == 0
    hardlink = hardlink_state['base'] / 'escaped-hardlink.pem'
    os.link(hardlink_state['pending_path'], hardlink)
    hardlink_archive = _run_qweather_manager(hardlink_state, 'archive')
    assert hardlink_archive.returncode == 64
    assert hardlink_state['pending_path'].exists()

    replacement_state = _create_qweather_manager_state(
        tmp_path, 'replacement-release'
    )
    assert _run_qweather_manager(
        replacement_state, 'provision', payload
    ).returncode == 0
    pending_path = replacement_state['pending_path']
    original_inode = pending_path.stat().st_ino
    replacement = pending_path.with_name(f'{pending_path.name}.replacement')
    replacement.write_bytes(payload)
    replacement.chmod(0o600)
    replacement_inode = replacement.stat().st_ino
    assert replacement_inode != original_inode
    os.replace(replacement, pending_path)
    assert pending_path.stat().st_ino == replacement_inode
    replacement_archive = _run_qweather_manager(replacement_state, 'archive')
    assert replacement_archive.returncode == 64
    assert replacement_state['pending_path'].exists()

    mismatch_state = _create_qweather_manager_state(tmp_path, 'mismatch-release')
    assert _run_qweather_manager(
        mismatch_state, 'provision', payload
    ).returncode == 0
    assert _run_qweather_manager(mismatch_state, 'archive').returncode == 0
    recovery = (
        mismatch_state['preactivation_root']
        / mismatch_state['release_id']
        / 'qweather-key-recovery'
        / 'pending.pem'
    )
    recovery_inode = recovery.stat().st_ino
    mismatch = _run_qweather_manager(
        mismatch_state,
        'provision',
        b'different-private-key\n',
    )
    assert mismatch.returncode == 64
    assert recovery.stat().st_ino == recovery_inode
    assert not mismatch_state['pending_path'].exists()


def test_qweather_preactivation_never_mutates_activation_adopted_key(tmp_path):
    state = _create_qweather_manager_state(tmp_path)
    payload = b'synthetic-activation-adoption\n'
    assert _run_qweather_manager(state, 'provision', payload).returncode == 0
    manifest = _load_qweather_manifest(state)
    pending_stat = state['pending_path'].stat()
    _write_activation_plan(state, manifest, 'activation-one')

    adopted = _run_qweather_manager(state, 'archive')
    assert adopted.returncode == 0, adopted.stderr.decode()
    assert adopted.stdout.strip() == b'activation-adopted'
    assert state['pending_path'].stat().st_ino == pending_stat.st_ino

    second_plan = _write_activation_plan(state, manifest, 'activation-two')
    concurrent = _run_qweather_manager(state, 'archive')
    assert concurrent.returncode == 64
    assert state['pending_path'].stat().st_ino == pending_stat.st_ino

    plan = json.loads(second_plan.read_text(encoding='utf-8'))
    plan['sha256'] = '0' * 64
    second_plan.write_text(
        json.dumps(plan, sort_keys=True, separators=(',', ':')) + '\n',
        encoding='utf-8',
    )
    second_plan.chmod(0o600)
    mismatch = _run_qweather_manager(state, 'archive')
    assert mismatch.returncode == 64
    assert state['pending_path'].stat().st_ino == pending_stat.st_ino


def test_qweather_preactivation_cleanup_covers_all_pre_activation_failures():
    content = _load_deploy_script()
    provision = content.index('\nprovision_qweather_jwt_private_key\n')
    activation = content.index(
        'QWEATHER_PENDING_KEY_PATH=$ACTIVATION_QWEATHER_PENDING_KEY_PATH '
        'bash $RELEASE_APP/scripts/activate_release.sh'
    )
    checkpoints = (
        'scripts/validate_release_env.py --file $STAGED_ENV_FILE '
        '--require-wechat $EFFECTIVE_REQUIRE_WECHAT_READY '
        '--require-weather-ready '
        '$REMOTE_QWEATHER_VALIDATION_PENDING_ARG"',
        'scripts/install_release_dependencies.sh',
        'scripts/release_runtime_smoke.py run',
        'systemd-analyze verify $NEW_RELEASE/systemd/*.service',
        '--seed-persistent-budget',
    )

    for checkpoint in checkpoints:
        assert provision < content.index(checkpoint, provision) < activation
    assert 'REMOTE_QWEATHER_PREACTIVATION_ACTIVE="1"' in content
    assert 'archive_qweather_preactivation_key || remote_cleanup_status=$?' in content
    assert 'REMOTE_QWEATHER_PREACTIVATION_ACTIVE="0"' in content[activation:]


def test_qweather_pending_key_is_validated_three_times_and_passed_to_activation():
    content = _load_deploy_script()
    validator_flag = (
        '--qweather-private-key-pending-path '
        '$REMOTE_QWEATHER_PENDING_KEY_PATH'
    )

    assert content.count(validator_flag) == 1
    assert content.count('$REMOTE_QWEATHER_VALIDATION_PENDING_ARG') == 3
    assert (
        'QWEATHER_PENDING_KEY_PATH=$ACTIVATION_QWEATHER_PENDING_KEY_PATH '
        'bash $RELEASE_APP/scripts/activate_release.sh'
    ) in content
    assert (
        'REMOTE_QWEATHER_VALIDATION_PENDING_ARG='
        '"--qweather-private-key-pending-path '
        '$REMOTE_QWEATHER_PENDING_KEY_PATH"'
    ) in content
    provision = content.index('\nprovision_qweather_jwt_private_key\n')
    assert provision < content.index(
        '$REMOTE_QWEATHER_VALIDATION_PENDING_ARG"',
        provision,
    )


def test_qweather_jwt_private_key_snapshot_rejects_unsafe_sources(tmp_path):
    private_key = tmp_path / 'qweather-ed25519.pem'
    subprocess.run(
        ['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(private_key)],
        check=True,
        capture_output=True,
        text=True,
    )
    private_key.chmod(0o600)
    symlink = tmp_path / 'qweather-link.pem'
    symlink.symlink_to(private_key)

    symlink_dir = tmp_path / 'symlink-snapshot'
    symlink_dir.mkdir(mode=0o700)
    symlink_result = _run_qweather_private_key_source_snapshotter(
        symlink, symlink_dir
    )
    assert symlink_result.returncode == 64
    assert '普通非符号链接文件' in symlink_result.stderr

    private_key.chmod(0o640)
    mode_dir = tmp_path / 'mode-snapshot'
    mode_dir.mkdir(mode=0o700)
    mode_result = _run_qweather_private_key_source_snapshotter(
        private_key, mode_dir
    )
    assert mode_result.returncode == 64
    assert '权限必须精确为 0600' in mode_result.stderr

    relative_dir = tmp_path / 'relative-snapshot'
    relative_dir.mkdir(mode=0o700)
    relative_result = _run_qweather_private_key_source_snapshotter(
        'relative.pem', relative_dir
    )
    assert relative_result.returncode == 64
    assert '必须使用绝对路径' in relative_result.stderr
    assert result_paths_not_exposed(
        (symlink_result, mode_result, relative_result),
        (private_key, symlink, relative_dir),
    )


def test_qweather_jwt_private_key_snapshot_rejects_other_algorithms(tmp_path):
    private_key = tmp_path / 'qweather-rsa.pem'
    subprocess.run(
        ['openssl', 'genpkey', '-algorithm', 'RSA', '-out', str(private_key)],
        check=True,
        capture_output=True,
        text=True,
    )
    private_key.chmod(0o600)

    deploy_temp_dir = tmp_path / 'rsa-snapshot'
    deploy_temp_dir.mkdir(mode=0o700)
    result = _run_qweather_private_key_source_snapshotter(
        private_key, deploy_temp_dir
    )

    assert result.returncode == 64
    assert '私钥快照必须是有效的 Ed25519 私钥' in result.stderr
    assert result.stdout == ''


def result_paths_not_exposed(results, paths):
    combined = ''.join(result.stdout + result.stderr for result in results)
    return all(str(path) not in combined for path in paths)


def test_validate_remote_path_rejects_noncanonical_segments():
    assert _run_remote_path_validator('/opt/case-weather').returncode == 0
    for value in (
        '//opt/case-weather',
        '/opt//case-weather',
        '/opt/./case-weather',
        '/opt/../case-weather',
        '/opt/case-weather/',
    ):
        result = _run_remote_path_validator(value)
        assert result.returncode != 0
        assert value not in result.stderr


def test_deploy_only_supports_key_or_sshpass_and_locks_private_files():
    content = _load_deploy_script()

    assert 'expect -c' not in content
    assert '密码部署需要 sshpass' in content
    assert content.count('UMask=0077') == 9
    assert 'chmod 0700 $PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run' in content


def test_deploy_runs_all_runtime_units_as_hardened_service_user():
    content = _load_deploy_script()

    assert content.count('User=root') == 1
    assert content.count('Group=root') == 1
    assert content.count('User=case-weather') == 5
    assert content.count('Group=case-weather') == 5
    for directive in (
        'NoNewPrivileges=true',
        'PrivateTmp=true',
        'PrivateDevices=true',
        'ProtectSystem=strict',
        'ProtectHome=true',
        'RestrictSUIDSGID=true',
        'RestrictNamespaces=true',
        'CapabilityBoundingSet=',
        'ReadWritePaths=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/run',
    ):
        expected_count = 5 if directive.startswith('ReadWritePaths=') else 6
        assert content.count(directive) == expected_count
    assert 'DISPATCH_LOCK_PATH=$PROJECT_DIR/run/case-weather-dispatch.lock' in content
    assert 'useradd --system' in content
    assert 'RUNTIME_USER=$RUNTIME_USER RUNTIME_GROUP=$RUNTIME_GROUP' in content
    activate = _load_activate_script()
    assert '--preserve-environment' not in activate
    assert 'runtime_exec "$VENV_DIR/bin/python" -m gunicorn' in activate
    assert 'require_executable "$VENV_DIR/bin/gunicorn"' not in activate
    assert 'CASE_WEATHER_FORMAL_SMOKE_LEASE_TOKEN=$FORMAL_SMOKE_LEASE_TOKEN' in activate
    assert 'CASE_WEATHER_FORMAL_SMOKE_LEASE_ACTION=$action' in activate
    assert '--renew-formal-lease-only' in activate
    assert '--release-formal-lease-only' in activate
    assert '/bin/bash scripts/weather_cache_sync.sh --skip-nowcast' in activate
    assert "local runtime_env=(\n        -i" in activate
    runtime_block = activate.split('runtime_exec() {', 1)[1].split('\n}', 1)[0]
    for inherited_secret in (
        'SSHPASS',
        'WX_MINIPROGRAM_SECRET',
        'QWEATHER_KEY',
        'SILICONFLOW_API_KEY',
        'WXPUSHER_APP_TOKEN',
    ):
        assert inherited_secret not in runtime_block


def test_deploy_generates_root_only_sandboxed_daily_backup_units():
    content = _load_deploy_script()
    activation = _load_activate_script()
    service_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-backup.service << 'EOF'"
    )
    timer_start = content.index(
        "cat > $NEW_RELEASE/systemd/case-weather-backup.timer << 'EOF'",
        service_start,
    )
    service_block = content[service_start:timer_start]
    timer_end = content.index('EOF"', timer_start)
    timer_block = content[timer_start:timer_end]

    for directive in (
        'Type=oneshot',
        'User=root',
        'Group=root',
        'PrivateNetwork=true',
        'NoNewPrivileges=true',
        'ProtectSystem=strict',
        'CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_SETUID CAP_SETGID',
        'RestrictAddressFamilies=AF_UNIX',
        'ReadOnlyPaths=$CURRENT_LINK $PROJECT_DIR/.env',
        'RequiresMountsFor=$PROJECT_DIR/instance $PROJECT_DIR/storage $PROJECT_DIR/backups/daily',
        'ReadWritePaths=$PROJECT_DIR/backups/daily $PROJECT_DIR/instance $PROJECT_DIR/storage',
        'InaccessiblePaths=$PROJECT_DIR/backups/deploy-transactions',
        'Environment=BACKUP_RUNTIME_USER=$RUNTIME_USER',
        'Environment=MKTEMP_BIN=mktemp',
        'Environment=INSTALL_BIN=install',
        'EnvironmentFile=$PROJECT_DIR/backups/backup-runtime.env',
        'ExecStart=/bin/bash $CURRENT_LINK/app/scripts/backup.sh',
        'TimeoutStartSec=15min',
        'ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress',
        'ConditionPathExists=|/run/case-weather/activation-permit',
    ):
        assert directive in service_block
    assert 'OnCalendar=*-*-* 03:00:00 Asia/Shanghai' in timer_block
    assert 'Persistent=true' in timer_block
    assert 'Unit=case-weather-backup.service' in timer_block
    assert 'ConditionPathExists=|!$PROJECT_DIR/deployments/activation-in-progress' in timer_block
    assert 'ConditionPathExists=|/run/case-weather/activation-permit' in timer_block
    assert 'systemctl systemd-run systemd-analyze busctl crontab pgrep runuser mktemp install findmnt sync' in content
    assert 'systemd-analyze verify $NEW_RELEASE/systemd/*.service $NEW_RELEASE/systemd/*.timer' in content
    assert 'InaccessiblePaths=$PROJECT_DIR/storage' not in service_block
    assert 'backup-validation.env' not in service_block
    assert '$CURRENT_LINK/app/scripts/backup.sh --if-present' not in service_block
    assert 'install_activation_guard_dropins' in activation
    assert 'for unit in "${ALL_UNITS[@]}"' in activation
    assert '步骤6.2.1: 给现有与新调度安装共享断电保护门' not in content
    for unit in (
        'case-weather.service',
        'case-weather-backup.service',
        'case-weather-backup.timer',
        'case-weather-cache.service',
        'case-weather-cache.timer',
        'case-weather-cache-bootstrap.service',
        'case-weather-cache-bootstrap.timer',
        'case-weather-dispatch.service',
        'case-weather-dispatch.timer',
        'case-weather-risk-precompute.service',
        'case-weather-risk-precompute.timer',
        'case-weather-usage-cleanup.service',
        'case-weather-usage-cleanup.timer',
        'case-weather-sync.service',
        'case-weather-sync.timer',
    ):
        assert unit in activation


def test_deploy_verifies_ssh_host_and_keeps_gunicorn_private():
    content = _load_deploy_script()

    assert 'StrictHostKeyChecking=yes' in content
    assert 'StrictHostKeyChecking=no' not in content
    assert 'UserKnownHostsFile=/dev/null' not in content
    assert '--bind 127.0.0.1:5000' in content
    assert '--bind 0.0.0.0:5000' not in content


def test_deploy_can_stage_formal_wechat_and_weather_readiness():
    content = _load_deploy_script()

    assert 'DEPLOY_MODE="${DEPLOY_MODE:-wechat_formal}"' in content
    assert 'web_backend_only)' in content
    assert 'wechat_formal)' in content
    assert 'DEPLOY_REQUIRE_WECHAT_READY' in content
    assert 'WECHAT_RELEASE_FORM_FILE' in content
    assert '--form-only' in content
    assert 'LOCAL_WECHAT_FORM_READY' in content
    assert 'remote_env_update "WX_MINIPROGRAM_APPID"' in content
    assert 'remote_env_update "WX_MINIPROGRAM_SECRET"' in content
    assert 'remote_env_generate_secret "WX_MINIPROGRAM_OPENID_PEPPER"' in content
    assert 'remote_env_generate_secret "WX_MINIPROGRAM_SESSION_SECRET"' in content
    assert 'remote_env_generate_secret "ACCOUNT_LINK_CODE_PEPPER"' in content
    assert 'ALLOW_WEATHER_UNAVAILABLE' in content


def test_preview_does_not_generate_partial_wechat_authentication():
    content = _load_deploy_script()

    assert 'WX_MINIPROGRAM_OPENID_PEPPER=\n' in content
    assert 'WX_MINIPROGRAM_SESSION_SECRET=\n' in content
    assert 'ACCOUNT_LINK_CODE_PEPPER=\n' in content
    assert 'WX_OPENID_PEPPER_GEN=' not in content
    assert 'WX_SESSION_SECRET_GEN=' not in content
    guard = 'if [ "$FORMAL_WECHAT_CONFIG_ALLOWED" = "1" ]; then'
    assert guard in content
    assert content.index(guard) < content.index(
        'remote_env_update "WX_MINIPROGRAM_SECRET"'
    )
    assert content.count('remote_env_generate_secret "WX_MINIPROGRAM_OPENID_PEPPER"') == 1
    assert content.count('remote_env_generate_secret "WX_MINIPROGRAM_SESSION_SECRET"') == 1
    assert content.count('remote_env_generate_secret "ACCOUNT_LINK_CODE_PEPPER"') == 1
    assert 'WX_MINIPROGRAM_APPID 与 WX_MINIPROGRAM_SECRET 必须由同一次发布同时提供。' in content


def test_explicit_credentials_rotate_and_auth_modes_clear_stale_values():
    content = _load_deploy_script()

    assert 'remote_env_update "QWEATHER_KEY" "$LOCAL_QWEATHER_KEY" "always"' in content
    assert 'remote_env_update "QWEATHER_API_BASE" "$LOCAL_QWEATHER_API_BASE" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_KID" "$LOCAL_QWEATHER_JWT_KID" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_PROJECT_ID" "$LOCAL_QWEATHER_JWT_PROJECT_ID" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_PRIVATE_KEY_PATH" "$LOCAL_QWEATHER_JWT_PRIVATE_KEY_PATH" "always"' in content
    assert 'remote_env_update "WXPUSHER_APP_TOKEN" "$LOCAL_WXPUSHER_APP_TOKEN" "always"' in content
    assert 'remote_env_update "FEATURE_WXPUSHER" "$LOCAL_FEATURE_WXPUSHER" "always"' in content
    assert 'remote_env_update "QWEATHER_KEY" "" "always"' in content
    assert 'remote_env_update "QWEATHER_JWT_KID" "" "always"' in content


def test_deploy_requires_exact_recovery_transaction_acknowledgement():
    content = _load_deploy_script()
    activate = _load_activate_script()

    assert 'DEPLOY_RECOVERY_ACKNOWLEDGED_TRANSACTION' in content
    assert 'RECOVERY_ACKNOWLEDGED_TRANSACTION=$RECOVERY_ACKNOWLEDGED_TRANSACTION' in content
    assert 'ROLLBACK_REQUIRED.txt' in activate
    assert 'RECOVERY_CONFIRMED' in activate
    assert '尚未人工确认的部署恢复事务' in activate


def test_activation_arms_qweather_gate_immediately_before_public_restart():
    content = _load_activate_script()

    assert '--key QWEATHER_NETWORK_NOT_BEFORE_EPOCH' in content
    assert 'not_before_epoch=$((now_epoch + 1800))' in content
    assert content.index('arm_qweather_network_gate\n') < content.index('start_new_release\n')
    assert content.index('start_candidate_release base\n') < content.index(
        'arm_qweather_network_gate\n'
    )
    assert content.index('start_candidate_release weather\n') < content.index(
        'arm_qweather_network_gate\n'
    )


def test_precompute_script_respects_deploy_venv_dir():
    content = _load_precompute_script()

    assert '${DEPLOY_VENV_DIR:+$DEPLOY_VENV_DIR/bin/python}' in content
    assert 'VENV_PY="${VENV_PY:-python3}"' in content


def test_implicit_wechat_formal_mode_rejects_preview_before_remote_command(
    tmp_path,
):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    remote_log = tmp_path / 'remote.log'
    _write_executable(
        fake_bin / 'ssh',
        '''#!/bin/bash
set -euo pipefail
payload="$(/bin/cat)"
{
    printf 'COMMAND'
    printf ' <%s>' "$@"
    printf '\nPAYLOAD=%s\n' "$payload"
} >> "$FAKE_DEPLOY_LOG"
''',
    )
    _write_executable(
        fake_bin / 'rsync',
        '''#!/bin/bash
set -euo pipefail
printf 'RSYNC' >> "$FAKE_DEPLOY_LOG"
printf ' <%s>' "$@" >> "$FAKE_DEPLOY_LOG"
printf '\n' >> "$FAKE_DEPLOY_LOG"
''',
    )
    form_file = tmp_path / 'wechat-release.env'
    form_file.write_text(
        '''WECHAT_FORM_READY=0
WX_MINIPROGRAM_APPID=wx-preview-canary
WX_MINIPROGRAM_SECRET=preview-secret-canary
WXPUSHER_APP_TOKEN=preview-wxpusher-canary
WX_MINIPROGRAM_PRIVACY_VERSION=preview-privacy-canary
FEATURE_HEAT_EXPOSURE_GIS=1
''',
        encoding='utf-8',
    )
    deploy_env = tmp_path / 'deploy.env'
    deploy_env.write_text(
        f'''DEPLOY_SERVER=fake.example
DEPLOY_USER=deployer
DEPLOY_PROJECT_DIR=/srv/case-weather
DEPLOY_RELEASE_ROOT=/srv/case-weather-deploy
DEPLOY_RELEASE_ID=preview-test
DEPLOY_LOCAL_DIR={ROOT}
DEPLOY_REQUIRE_WECHAT_READY=0
WECHAT_RELEASE_FORM_FILE={form_file}
''',
        encoding='utf-8',
    )
    environment = os.environ.copy()
    environment.pop('DEPLOY_MODE', None)
    environment.update(
        {
            'ENV_FILE': str(deploy_env),
            'WECHAT_RELEASE_FORM_FILE': str(form_file),
            'FAKE_DEPLOY_LOG': str(remote_log),
            'PATH': f"{fake_bin}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'deploy.sh')],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert (
        'DEPLOY_MODE=wechat_formal 必须同时设置 '
        'DEPLOY_REQUIRE_WECHAT_READY=1'
    ) in result.stderr
    assert not remote_log.exists()
    remote_text = ''
    for canary in (
        'wx-preview-canary',
        'preview-secret-canary',
        'preview-wxpusher-canary',
        'preview-privacy-canary',
    ):
        assert canary not in remote_text
    assert '--key WX_MINIPROGRAM_OPENID_PEPPER' not in remote_text
    assert '--key WX_MINIPROGRAM_SESSION_SECRET' not in remote_text


def test_explicit_web_backend_mode_reaches_remote_without_wechat_form(
    tmp_path,
):
    source = _create_clean_git_source(tmp_path)
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    remote_log = tmp_path / 'remote.log'
    _write_executable(
        fake_bin / 'ssh',
        '''#!/bin/bash
set -euo pipefail
{
    printf 'COMMAND'
    printf ' <%s>' "$@"
    printf '\\n'
} >> "$FAKE_DEPLOY_LOG"
exit 73
''',
    )
    missing_form = tmp_path / 'missing-wechat-release.env'
    deploy_env = tmp_path / 'web-backend.env'
    deploy_env.write_text(
        f'''DEPLOY_SERVER=fake.example
DEPLOY_USER=deployer
DEPLOY_PROJECT_DIR=/srv/case-weather
DEPLOY_RELEASE_ROOT=/srv/case-weather-deploy
DEPLOY_RELEASE_ID=web-backend-test
DEPLOY_LOCAL_DIR={source}
ML_MODEL_ARTIFACT_DIR={tmp_path / 'model-artifacts'}
DEPLOY_MODE=web_backend_only
DEPLOY_REQUIRE_WECHAT_READY=0
WECHAT_RELEASE_FORM_FILE={missing_form}
''',
        encoding='utf-8',
    )
    environment = os.environ.copy()
    environment.update(
        {
            'ENV_FILE': str(deploy_env),
            'FAKE_DEPLOY_LOG': str(remote_log),
            'PATH': f"{fake_bin}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'deploy.sh')],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 73
    assert '步骤1: 测试服务器连接' in result.stdout
    assert remote_log.exists()
    assert 'missing-wechat-release.env' not in result.stdout
    assert 'missing-wechat-release.env' not in result.stderr


def test_web_backend_mode_cannot_claim_wechat_ready(tmp_path):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    remote_log = tmp_path / 'remote.log'
    _write_executable(
        fake_bin / 'ssh',
        '''#!/bin/bash
printf 'unexpected remote call\\n' >> "$FAKE_DEPLOY_LOG"
''',
    )
    deploy_env = tmp_path / 'invalid-web-backend.env'
    deploy_env.write_text(
        '''DEPLOY_SERVER=fake.example
DEPLOY_USER=deployer
DEPLOY_MODE=web_backend_only
DEPLOY_REQUIRE_WECHAT_READY=1
''',
        encoding='utf-8',
    )
    environment = os.environ.copy()
    environment.update(
        {
            'ENV_FILE': str(deploy_env),
            'FAKE_DEPLOY_LOG': str(remote_log),
            'PATH': f"{fake_bin}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'deploy.sh')],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert (
        'DEPLOY_MODE=web_backend_only 必须保持 '
        'DEPLOY_REQUIRE_WECHAT_READY=0'
    ) in result.stderr
    assert not remote_log.exists()


def test_command_line_web_mode_cannot_be_overridden_by_env_file(tmp_path):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    remote_log = tmp_path / 'remote.log'
    _write_executable(
        fake_bin / 'ssh',
        '''#!/bin/bash
printf 'unexpected remote call\\n' >> "$FAKE_DEPLOY_LOG"
''',
    )
    deploy_env = tmp_path / 'stale-formal.env'
    deploy_env.write_text(
        '''DEPLOY_SERVER=fake.example
DEPLOY_USER=deployer
DEPLOY_MODE=wechat_formal
DEPLOY_REQUIRE_WECHAT_READY=1
WECHAT_RELEASE_FORM_FILE=/private/form-canary
''',
        encoding='utf-8',
    )
    environment = os.environ.copy()
    environment.update(
        {
            'ENV_FILE': str(deploy_env),
            'DEPLOY_MODE': 'web_backend_only',
            'DEPLOY_REQUIRE_WECHAT_READY': '0',
            'FAKE_DEPLOY_LOG': str(remote_log),
            'PATH': f"{fake_bin}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'deploy.sh')],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert '命令行 DEPLOY_MODE 与 ENV_FILE 冲突' in result.stderr
    assert '/private/form-canary' not in result.stdout
    assert '/private/form-canary' not in result.stderr
    assert not remote_log.exists()


def test_web_backend_mode_rejects_dirty_source_before_remote_command(
    tmp_path,
):
    source = _create_clean_git_source(tmp_path)
    (source / 'untracked.txt').write_text('dirty\n', encoding='utf-8')
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    remote_log = tmp_path / 'remote.log'
    _write_executable(
        fake_bin / 'ssh',
        '''#!/bin/bash
printf 'unexpected remote call\\n' >> "$FAKE_DEPLOY_LOG"
''',
    )
    deploy_env = tmp_path / 'dirty-web-backend.env'
    deploy_env.write_text(
        f'''DEPLOY_SERVER=fake.example
DEPLOY_USER=deployer
DEPLOY_LOCAL_DIR={source}
ML_MODEL_ARTIFACT_DIR={tmp_path / 'model-artifacts'}
DEPLOY_MODE=web_backend_only
DEPLOY_REQUIRE_WECHAT_READY=0
''',
        encoding='utf-8',
    )
    environment = os.environ.copy()
    environment.update(
        {
            'ENV_FILE': str(deploy_env),
            'FAKE_DEPLOY_LOG': str(remote_log),
            'PATH': f"{fake_bin}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'deploy.sh')],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert 'Git 工作树保持干净' in result.stderr
    assert not remote_log.exists()
