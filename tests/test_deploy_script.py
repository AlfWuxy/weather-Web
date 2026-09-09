# -*- coding: utf-8 -*-
"""部署脚本回归测试。"""

from pathlib import Path


def _load_deploy_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
    return script_path.read_text(encoding="utf-8")


def _load_precompute_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "community_risk_precompute.sh"
    return script_path.read_text(encoding="utf-8")


def test_deploy_script_checks_units_with_is_active():
    content = _load_deploy_script()

    assert 'check_remote_unit_active "case-weather"' in content
    assert 'check_remote_unit_active "case-weather-cache.timer"' in content
    assert 'check_remote_unit_active "case-weather-dispatch.timer"' in content
    assert 'check_remote_unit_active "case-weather-risk-precompute.timer"' in content
    assert 'systemctl is-active --quiet $unit' in content


def test_deploy_script_no_longer_swallows_systemctl_failures():
    content = _load_deploy_script()

    assert 'case-weather && systemctl restart case-weather && systemctl status --no-pager case-weather || true' not in content
    assert 'case-weather-dispatch.timer && systemctl status --no-pager case-weather-dispatch.timer || true' not in content
    assert 'case-weather-risk-precompute.timer && systemctl status --no-pager case-weather-risk-precompute.timer || true' not in content


def test_deploy_script_pins_duchang_cache_to_free_tier_budget():
    content = _load_deploy_script()

    assert 'WEATHER_SYNC_LOCATIONS=都昌县' in content
    assert 'QWEATHER_CANONICAL_LOCATION=116.20,29.27' in content
    assert 'QWEATHER_MONTHLY_REQUEST_LIMIT=40000' in content
    assert 'OnUnitActiveSec=30min' in content
    assert 'ExecStart=/bin/bash $PROJECT_DIR/scripts/weather_cache_sync.sh' in content


def test_deploy_script_sets_precompute_python_path():
    content = _load_deploy_script()

    assert 'Environment=VENV_PY=$VENV_DIR/bin/python' in content


def test_deploy_script_uses_shared_database_backup_resolver():
    content = _load_deploy_script()

    assert "bash scripts/backup.sh --if-present" in content
    assert "PROJECT_DIR='$PROJECT_DIR'" in content
    assert "ENV_FILE='$PROJECT_DIR/.env'" in content
    assert "cp -a instance/health_weather.db" not in content
    assert "redis-server sqlite3" in content


def test_deploy_script_excludes_local_design_drafts():
    content = _load_deploy_script()

    assert "--exclude '.claude'" in content
    assert "--exclude '.superpowers'" in content
    assert "--exclude '.pytest_cache'" in content
    assert "--exclude 'backups'" in content
    assert "--exclude 'output'" in content
    assert "--exclude 'tmp'" in content
    assert "--exclude 'blueprints/tools 2.py'" in content


def test_deploy_script_excludes_root_analysis_dir():
    content = _load_deploy_script()
    flag = "--exclude=/analysis/"

    assert content.count(flag) == 3
    assert "--exclude '/analysis/'" not in content

    rsync_starts = [idx for idx in range(len(content)) if content.startswith("rsync -avz", idx)]
    assert len(rsync_starts) == 3
    for start in rsync_starts:
        end = content.find("$PROJECT_DIR/", start)
        assert end != -1
        assert flag in content[start:end]


def test_deploy_script_requires_https_public_base_url():
    content = _load_deploy_script()

    assert 'ALLOW_INSECURE_PUBLIC_BASE_URL' in content
    assert 'PUBLIC_BASE_URL 必须使用 HTTPS' in content
    assert 'ALLOW_INSECURE_PUBLIC_BASE_URL=1' in content
    assert 'DEFAULT_PUBLIC_BASE_URL="http://$SERVER:5000"' in content


def test_deploy_script_binds_localhost_not_all_interfaces():
    content = _load_deploy_script()
    assert '--bind 127.0.0.1:5000' in content
    assert '--bind 0.0.0.0:5000' not in content


def test_deploy_script_ssh_host_key_not_disabled():
    """P11：默认不得 StrictHostKeyChecking=no + known_hosts=/dev/null。"""
    content = _load_deploy_script()
    # 默认串使用 accept-new；允许环境覆盖，但仓库默认不得写死 no+/dev/null 组合
    assert 'StrictHostKeyChecking=accept-new' in content
    assert 'UserKnownHostsFile=/dev/null' not in content.split('DEFAULT_SSH_OPTS')[1].split('\n')[0]
    assert 'DEPLOY_APP_USER' in content
    assert '源站仅本机' in content or '127.0.0.1:5000' in content


def test_precompute_script_respects_deploy_venv_dir():
    content = _load_precompute_script()

    assert '${DEPLOY_VENV_DIR:+$DEPLOY_VENV_DIR/bin/python}' in content
    assert 'VENV_PY="${VENV_PY:-python3}"' in content
