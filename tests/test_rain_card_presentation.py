# -*- coding: utf-8 -*-
"""共享降雨卡接入各今日页，并执行浏览器数值呈现规则测试。"""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_rainfall_numeric_contract_in_browser_code():
    node = shutil.which('node')
    if not node:
        pytest.skip('需要 Node.js 执行实际前端雨量计算契约')
    result = subprocess.run([node, '--test', str(ROOT / 'tests/rain_card.test.cjs')],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('path', ['/risk'])
def test_anonymous_today_pages_share_rainfall_card(client, db_session, monkeypatch, path):
    monkeypatch.setattr('services.public_service.get_weather_with_cache', lambda _: ({'is_mock': True}, False))
    response = client.get(path)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert body.count('data-rain-card') == 1
    assert '时段雨量预报：未知' in body
    assert 'data-rain-rows' in body
    assert 'rain-card.js' in body
    assert 'mm/h' in body
    assert '不能用作中国 24 小时雨量等级或官方预警等级' in body
    assert 'weather/nowcast' in body and 'hours=6' in body


@pytest.mark.parametrize('path', ['/dashboard', '/elder-mode'])
def test_logged_in_today_pages_use_same_live_rainfall_card(authenticated_client, monkeypatch, path):
    monkeypatch.setattr('services.user.dashboard_service.get_weather_with_cache', lambda _: ({'is_mock': True}, False))
    monkeypatch.setattr('services.user.dashboard_service._dashboard_forecast_days', lambda *a, **k: [])
    body = authenticated_client.get(path).get_data(as_text=True)
    assert body.count('data-rain-card') == 1
    assert 'data-rain-amount' in body and 'data-rain-intensity' in body
    assert 'rain-card.js' in body
