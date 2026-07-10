# -*- coding: utf-8 -*-
"""指标解释目录、页面入口和交互资源的回归测试。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_metric_catalog_is_complete_and_has_unique_anchors():
    from core.metric_explanations import (
        METRIC_EXPLANATION_GROUPS,
        METRIC_EXPLANATIONS,
    )

    required_fields = {
        'anchor',
        'title',
        'summary',
        'formula',
        'variables',
        'thresholds',
        'method',
        'window',
        'missing',
        'limitations',
        'source_file',
    }
    grouped_keys = [
        key
        for group in METRIC_EXPLANATION_GROUPS
        for key in group['keys']
    ]

    assert set(grouped_keys) == set(METRIC_EXPLANATIONS)
    assert len(grouped_keys) == len(set(grouped_keys))
    anchors = [metric['anchor'] for metric in METRIC_EXPLANATIONS.values()]
    assert len(anchors) == len(set(anchors))
    for metric in METRIC_EXPLANATIONS.values():
        assert required_fields <= set(metric)
        assert metric['formula']
        assert metric['limitations']


def test_transparency_page_renders_formula_index(client):
    response = client.get('/transparency')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '每一个风险数字都应该能被解释' in body
    assert 'Score = 100 × [0.50×HI_norm + 0.30×Night_norm + 0.20×Streak_norm]' in body
    assert 'id="community-risk-index"' in body
    assert 'id="sir"' in body
    assert '缺失值处理' in body
    assert '已知局限' in body
    assert 'Open-Meteo' in body
    assert 'metric-explanations.js' in body


def test_public_risk_exposes_current_inputs_in_info_button(client, monkeypatch):
    weather = {
        'temperature': 36.0,
        'temperature_max': 38.0,
        'temperature_min': 28.0,
        'humidity': 72.0,
        'data_source': 'QWeather',
        'is_mock': False,
    }
    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda _location: (weather, False),
    )
    monkeypatch.setattr(
        'services.public_service.get_consecutive_hot_days',
        lambda _location, today_max=None: 3,
    )

    response = client.get('/risk?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'data-metric-info="heat_risk_score"' in body
    assert 'data-metric-info="heat_index"' in body
    assert 'data-metric-info="personal_threshold"' in body
    assert '连续高温' in body
    assert '3天' in body
    assert 'aria-label="查看' in body
    assert '系统不存老人姓名、电话、慢病或精确住址' not in body
    assert '主动填写的信息会保存在服务器' in body


def test_public_risk_fails_closed_for_mock_weather(client, monkeypatch):
    weather = {
        'temperature': 20.0,
        'temperature_max': 25.0,
        'temperature_min': 15.0,
        'humidity': 60.0,
        'data_source': 'Demo',
        'is_mock': True,
    }
    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda _location: (weather, False),
    )

    response = client.get('/risk?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '等待真实天气' in body
    assert '当前风险：低风险' not in body
    assert '综合评分 0.0' not in body
    assert '本页已停止生成风险等级、综合分和行动清单' in body


def test_public_risk_fails_closed_when_required_weather_field_is_missing(client, monkeypatch):
    weather = {
        'temperature': 36.0,
        'temperature_max': 38.0,
        'temperature_min': None,
        'humidity': 72.0,
        'data_source': 'QWeather',
        'is_mock': False,
    }
    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda _location: (weather, False),
    )

    response = client.get('/risk?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '等待真实天气' in body
    assert '当前风险：' not in body


def test_metric_info_script_supports_hover_focus_click_and_escape():
    script = (ROOT / 'static/js/metric-explanations.js').read_text(encoding='utf-8')

    assert "trigger: 'hover focus click'" in script
    assert 'instance._activeTrigger' in script
    assert "event.key !== 'Escape'" in script
    assert 'MutationObserver' in script
    assert 'escapeHtml' in script
