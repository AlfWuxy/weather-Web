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
    assert 'id="community-screening-score"' in body
    assert 'id="sir"' in body
    assert 'id="gis-native-grid"' in body
    assert 'id="gis-lst-mean"' in body
    assert 'id="gis-validation"' in body
    assert 'LST_C = Raw×0.02−273.15；Mean = ΣLST_C / n_Q3' in body
    assert 'Score = [P(age65_share_pct) + P(q3_lst_c_mean) + P(100−tree_cover_pct)] / 3' in body
    assert 'Publish = 1{validation_pass = true ∧ status = pass ∧ hard_failures = 0}' in body
    assert '缺失值处理' in body
    assert '已知局限' in body
    assert 'Open-Meteo' in body
    assert 'metric-explanations.js' in body
    assert '探索性空间筛查轨与完整画像风险轨分开运行' in body
    assert '探索性筛查得分只用于安排后续核查与数据收集顺序' in body
    assert '不授权医疗资源配置' in body
    assert '来源证据状态：HOLD' in body
    assert '现有程序尚未机器核验字段来源、单位、观测时点、社区标识或基线时间窗口' in body
    assert '稳定的中性代理值' not in body
    assert '通过来源、单位和有效值核验' not in body


def test_public_risk_exposes_current_inputs_in_info_button(
    app,
    client,
    db_session,
):
    from core.time_utils import utcnow
    from services.miniprogram_service import persist_snapshot

    weather = {
        'temperature': 36.0,
        'temperature_max': 38.0,
        'temperature_min': 28.0,
        'humidity': 72.0,
        'pressure': 1002.0,
        'weather_condition': '晴',
        'wind_speed': 2.0,
        'observed_at': utcnow().isoformat(),
        'quality_version': 1,
        'data_source': 'QWeather',
        'is_mock': False,
        'consecutive_hot_days': 3,
    }
    with app.app_context():
        persist_snapshot(weather, [], [], fetched_at=utcnow())

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
    assert '你主动填写的账户和家庭资料会保存在服务器' in body


def test_public_risk_fails_closed_for_mock_weather(
    app,
    client,
    db_session,
):
    from core.time_utils import utcnow
    from services.miniprogram_service import persist_snapshot

    weather = {
        'temperature': 20.0,
        'temperature_max': 25.0,
        'temperature_min': 15.0,
        'humidity': 60.0,
        'data_source': 'Demo',
        'is_mock': True,
    }
    with app.app_context():
        persist_snapshot(weather, [], [], fetched_at=utcnow())

    response = client.get('/risk?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '天气更新中' in body
    assert '当前风险：低风险' not in body
    assert '综合评分 0.0' not in body
    assert '风险等级暂不显示' in body
    assert '附近避暑资源' in body


def test_public_risk_fails_closed_when_required_weather_field_is_missing(
    app,
    client,
    db_session,
):
    from core.time_utils import utcnow
    from services.miniprogram_service import persist_snapshot

    weather = {
        'temperature': 36.0,
        'temperature_max': 38.0,
        'temperature_min': None,
        'humidity': 72.0,
        'data_source': 'QWeather',
        'is_mock': False,
    }
    with app.app_context():
        persist_snapshot(weather, [], [], fetched_at=utcnow())

    response = client.get('/risk?location=都昌')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '天气更新中' in body
    assert '当前风险：' not in body


def test_metric_info_script_supports_single_popover_and_keyboard_escape():
    script = (ROOT / 'static/js/metric-explanations.js').read_text(encoding='utf-8')

    assert "trigger: 'hover click'" in script
    assert "trigger: 'hover focus click'" not in script
    assert 'instance._activeTrigger' in script
    assert "event.key !== 'Escape'" in script
    assert 'activeController' in script
    assert "tip.setAttribute('role', 'dialog')" in script
    assert "button.setAttribute('aria-controls', tip.id)" in script
    assert 'focusTarget.focus({ preventScroll: true })' in script
    assert "if (event.key === 'Enter' || event.key === ' ')" in script
    assert "if (event.key === 'Tab')" in script
    assert 'const leavesPopover' in script
    assert 'instance.show()' in script
    assert 'controller.close(true)' in script
    assert "document.addEventListener('pointerdown'" in script
    assert 'button.blur()' not in script
    assert 'MutationObserver' in script
    assert 'escapeHtml' in script


def test_metric_popover_is_scrollable_on_narrow_screens():
    css = (ROOT / 'static/css/yilao.css').read_text(encoding='utf-8')

    assert 'position: fixed !important' in css
    assert 'inset: auto 12px 12px !important' in css
    assert 'max-height: calc(100dvh - 24px - env(safe-area-inset-bottom, 0px))' in css
    assert 'flex-direction: column' in css
    assert 'overflow-y: auto' in css
    assert 'overscroll-behavior: contain' in css
    assert '.yl-metric-popover .popover-arrow' in css
    assert 'display: none' in css
