# -*- coding: utf-8 -*-
"""行动交接、老人入口、页脚与避暑页的 P0 展示回归。"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _login_as(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True


def test_miniprogram_action_code_image_accepts_only_static_relative_or_https():
    from core.config import _validated_miniprogram_action_code_image

    assert _validated_miniprogram_action_code_image('') == ''
    assert (
        _validated_miniprogram_action_code_image('static/brand/yilao-avatar.png')
        == 'static/brand/yilao-avatar.png'
    )
    assert _validated_miniprogram_action_code_image(
        'static/brand/missing-action-code.png'
    ) == ''
    assert (
        _validated_miniprogram_action_code_image('https://cdn.example/action-code.png')
        == 'https://cdn.example/action-code.png'
    )
    for invalid in (
        '/static/brand/action-code.png',
        'static/../secrets.txt',
        'http://cdn.example/action-code.png',
        'javascript:alert(1)',
        'https://user:pass@cdn.example/action-code.png',
    ):
        with pytest.raises(RuntimeError, match='WX_MINIPROGRAM_ACTION_CODE_IMAGE'):
            _validated_miniprogram_action_code_image(invalid)


def test_formal_action_handoff_has_real_steps_and_no_fake_qr(client, app):
    app.config['WECHAT_FORMAL_RUNTIME'] = True
    app.config['WX_MINIPROGRAM_ACTION_CODE_IMAGE'] = ''

    response = client.get('/action')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '宜老平安' in body
    assert '宜老天气通' in body
    assert 'pages/actions/index' in body
    assert '在“照护”中选择家人' in body
    assert '微信搜索' in body
    assert 'name="short_code"' not in body
    assert 'data-testid="miniprogram-action-code"' not in body


def test_formal_action_handoff_renders_valid_configured_qr(client, app):
    app.config['WECHAT_FORMAL_RUNTIME'] = True
    app.config['WX_MINIPROGRAM_ACTION_CODE_IMAGE'] = 'static/brand/yilao-avatar.png'

    response = client.get('/action')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data-testid="miniprogram-action-code"' in body
    assert re.search(r'src="/static/brand/yilao-avatar\.png(?:\?v=\d+)?"', body)


def test_formal_forward_messages_remove_dead_web_link_and_short_code(app):
    from services.user._helpers import (
        _build_caregiver_message,
        _build_community_message,
    )
    from services.user.caregiver_service import _build_weather_waiting_message

    pair = SimpleNamespace(
        location_query='都昌县',
        community_code='都昌县',
        short_code='654321',
    )
    with app.app_context():
        app.config['WECHAT_FORMAL_RUNTIME'] = True
        caregiver = _build_caregiver_message(
            pair,
            alert_kind='heat',
            weather_data={'temperature_max': 36},
            action_link='https://yilaoweather.org/action?short_code=654321',
        )
        community = _build_community_message('都昌县', '高风险', [])
        waiting = _build_weather_waiting_message(
            pair,
            'https://yilaoweather.org/action?short_code=654321',
        )

    for message in (caregiver, community, waiting):
        assert '宜老平安' in message
        assert '宜老天气通' in message
        assert 'pages/actions/index' in message
        assert 'https://yilaoweather.org/action' not in message
        assert '654321' not in message
        assert '短码' not in message


def test_web_only_forward_message_keeps_legacy_link_and_short_code(app):
    from services.user._helpers import _build_caregiver_message

    pair = SimpleNamespace(
        location_query='都昌县',
        community_code='都昌县',
        short_code='654321',
    )
    with app.app_context():
        app.config['WECHAT_FORMAL_RUNTIME'] = False
        message = _build_caregiver_message(
            pair,
            action_link='https://yilaoweather.org/action?short_code=654321',
        )

    assert 'https://yilaoweather.org/action?short_code=654321' in message
    assert '短码：654321' in message


def test_role_entry_no_longer_promises_web_care_code(client, app):
    app.config['WECHAT_FORMAL_RUNTIME'] = True

    response = client.get('/entry')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '照护码完成今日确认或求助' not in body
    assert '宜老平安' in body
    assert '宜老天气通' in body


def test_elder_page_prioritizes_risk_and_confirms_calls(
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import FamilyMember, FamilyMemberProfile, User

    user = User(username='elder-ui-user', role='user', community='都昌')
    user.set_password('a-valid-password')
    db_session.add(user)
    db_session.flush()
    member = FamilyMember(user_id=user.id, name='父亲', relation='父亲', age=76)
    db_session.add(member)
    db_session.flush()
    db_session.add(FamilyMemberProfile(
        member_id=member.id,
        contact_prefs=json.dumps({
            'emergency_name': '家人小吴',
            'emergency_phone': '13800138000',
        }, ensure_ascii=False),
    ))
    db_session.commit()
    _login_as(client, user)

    monkeypatch.setattr(
        'services.user.dashboard_service.get_weather_with_cache',
        lambda location: ({
            'temperature': 35.5,
            'temperature_max': 38,
            'temperature_min': 28,
            'humidity': 70,
            'pressure': 1002,
            'weather_condition': '晴',
            'wind_speed': 1.5,
            'aqi': 60,
            'is_mock': False,
            'data_source': 'QWeather',
        }, False),
    )
    monkeypatch.setattr(
        'services.user.dashboard_service.get_qweather_forecast_with_cache',
        lambda location, days=7: ([], False, {'error': 'unavailable'}),
    )

    response = client.get('/elder-mode')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '今日风险等级' in body
    assert re.search(r'\d+(?:\.0)?\s*分\s*</span>', body)
    assert 'type="button"' in body
    assert 'data-bs-target="#contactCallConfirm"' in body
    assert 'data-bs-target="#emergencyCallConfirm"' in body
    assert 'href="tel:13800138000"' in body
    assert 'href="tel:120"' in body
    assert '<noscript>' in body
    assert '直接联系 家人小吴' in body
    assert '紧急情况直接拨打 120' in body
    assert '回到今天' in body
    assert re.search(
        r'class="btn btn-outline-primary btn-lg" href="/elder-mode"[^>]*>.*?回到今天',
        body,
        re.DOTALL,
    )
    assert body.count('href="/elder-mode" data-nav-key="today"') == 2
    assert re.search(
        r'class="nav-link active"\s+href="/elder-mode" data-nav-key="today"\s+aria-current="page">今天</a>',
        body,
    )
    assert body.count('href="/elder-mode" data-nav-key="today" aria-current="page"') == 1
    assert '/static/css/elder-fix.css' in body

    family_response = client.get('/dashboard')
    family_body = family_response.get_data(as_text=True)
    assert family_response.status_code == 200
    assert family_body.count('href="/dashboard" data-nav-key="today"') == 2


def test_mobile_touch_target_styles_cover_controls_without_expanding_body_links():
    polish_css = (
        PROJECT_ROOT / 'static/css/apple-polish.css'
    ).read_text(encoding='utf-8')
    elder_css = (
        PROJECT_ROOT / 'static/css/elder-fix.css'
    ).read_text(encoding='utf-8')

    assert '@media (max-width: 767.98px)' in polish_css
    assert '.btn {' in polish_css
    assert 'min-width: 44px;' in polish_css
    assert 'min-height: 44px;' in polish_css
    assert '.navbar-toggler {' in polish_css
    assert '.btn-close {' in polish_css
    assert '.yl-metric-info {' in polish_css
    assert '.site-footer nav a,' in polish_css
    assert '.site-footer p a {' in polish_css
    assert 'details > summary {' in polish_css
    assert '.form-check {' in polish_css
    assert '.form-check-label {' in polish_css
    assert '.form-check .form-check-input {' in polish_css
    assert 'width: 24px;' in polish_css
    form_control_rule = re.search(
        r'\.form-check \.form-check-input\s*\{(?P<body>.*?)\}',
        polish_css,
        re.DOTALL,
    )
    assert form_control_rule is not None
    assert 'opacity:' not in form_control_rule.group('body')
    assert '.page-shell a {' not in polish_css
    assert 'main a {' not in polish_css

    assert 'body.elder-focus-page .yl-metric-info {' in elder_css
    assert 'body.elder-focus-page .navbar-toggler {' in elder_css
    assert 'body.elder-focus-page .btn-close {' in elder_css
    assert 'body.elder-focus-page .site-footer nav a {' in elder_css


def test_formal_wechat_template_uses_miniprogram_copy_label():
    template = (
        PROJECT_ROOT / 'templates/caregiver_wechat_template.html'
    ).read_text(encoding='utf-8')

    assert 'elif formal_action_handoff %}复制小程序操作说明' in template


def test_cooling_first_frame_and_filters_are_truthful(client, db_session, monkeypatch):
    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: ({
            'temperature': 27.5,
            'is_mock': False,
            'data_source': 'QWeather',
        }, False),
    )

    response = client.get('/cooling')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '<span class="num">27.5</span>' in body
    assert '<span class="num">0</span>' not in body
    assert 'for="coolingCommunity"' in body
    assert 'id="coolingCommunity"' in body
    assert re.search(
        r'<input[^>]+id="coolingCommunity"[^>]+name="community"[^>]+value=""',
        body,
    )
    assert 'for="coolingResourceType"' in body
    assert 'id="coolingResourceType"' in body
    assert 'mailto:wuxy.alf2024@gdhfi.com' in body
    assert '共建避暑资源' in body


def test_footer_discloses_operator_type_and_contact(client):
    response = client.get('/')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '运营主体：个人' in body
    assert 'mailto:wuxy.alf2024@gdhfi.com' in body
    assert '联系邮箱：wuxy.alf2024@gdhfi.com' in body


def test_base_logout_links_to_confirmation_page():
    template = (PROJECT_ROOT / 'templates/base.html').read_text(encoding='utf-8')

    assert template.count("href=\"{{ url_for('public.logout') }}\"") >= 2
    assert "action=\"{{ url_for('public.logout') }}\"" not in template
