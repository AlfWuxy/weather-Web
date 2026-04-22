# -*- coding: utf-8 -*-
"""部署脚本回归测试。"""

from pathlib import Path


def _load_deploy_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
    return script_path.read_text(encoding="utf-8")


def test_deploy_script_checks_units_with_is_active():
    content = _load_deploy_script()

    assert 'check_remote_unit_active "case-weather"' in content
    assert 'check_remote_unit_active "case-weather-dispatch.timer"' in content
    assert 'check_remote_unit_active "case-weather-risk-precompute.timer"' in content
    assert 'systemctl is-active --quiet $unit' in content


def test_deploy_script_no_longer_swallows_systemctl_failures():
    content = _load_deploy_script()

    assert 'case-weather && systemctl restart case-weather && systemctl status --no-pager case-weather || true' not in content
    assert 'case-weather-dispatch.timer && systemctl status --no-pager case-weather-dispatch.timer || true' not in content
    assert 'case-weather-risk-precompute.timer && systemctl status --no-pager case-weather-risk-precompute.timer || true' not in content
