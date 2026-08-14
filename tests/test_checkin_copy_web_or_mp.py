# -*- coding: utf-8 -*-
"""P5：复制话术必须诚实写明网页短码确认，且 Web / 小程序三路同步。"""
from pathlib import Path
from types import SimpleNamespace

from core.db_models import Community, Pair, User
from core.security import hash_short_code
from core.time_utils import utcnow


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_CONFIRM = '可在网页行动页输入短码完成确认。'
MP_VIEW = '微信小程序也可查看提醒。'
FORBIDDEN_COPY = (
    '确认需在微信小程序完成',
    '可在微信小程序完成确认',
)
PAIR_MANAGEMENT_FAIL_CLOSED = '风险等级暂不显示。仍可发送行动链接并记录确认结果。'
LISTED_COPY_SITES = (
    'services/user/caregiver_service.py',
    'services/user/_helpers.py',
    'services/user/community_service.py',
    'miniprogram/pages/template/index.js',
    'templates/caregiver_wechat_template.html',
    'templates/community_wechat.html',
)

MOCK_WEATHER = {
    'temperature': 37.0,
    'temperature_max': 39.0,
    'temperature_min': 29.0,
    'humidity': 70.0,
    'data_source': 'Demo',
    'is_mock': True,
    'is_demo': True,
}
REAL_WEATHER = {
    'temperature': 37.0,
    'temperature_max': 39.0,
    'temperature_min': 29.0,
    'humidity': 70.0,
    'data_source': 'QWeather',
    'is_mock': False,
}


def _login_as(client, user_id, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def _create_user(db_session, username, role, community='都昌'):
    user = User(username=username, role=role, community=community)
    user.set_password('checkin-copy-test-password')
    db_session.add(user)
    db_session.commit()
    return user


def _create_pair(db_session, user_id, short_code='27182818'):
    pair = Pair(
        caregiver_id=user_id,
        community_code='都昌',
        location_query='都昌',
        elder_code=f'elder-{short_code}',
        short_code=short_code,
        short_code_hash=hash_short_code(short_code),
        status='active',
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.commit()
    return pair


def _patch_caregiver_location(monkeypatch):
    monkeypatch.setattr(
        'services.user.caregiver_service.resolve_location',
        lambda _label: {
            'location_code': '101240201',
            'display_name': '都昌',
        },
    )


def _read(relpath):
    return (REPO_ROOT / relpath).read_text(encoding='utf-8')


def test_listed_copy_sites_all_share_web_short_code_sentence():
    """漏改任一列出的站点就会让网页与小程序话术分叉。"""
    for relpath in LISTED_COPY_SITES:
        text = _read(relpath)
        assert WEB_CONFIRM in text, f'{relpath} 缺少网页短码确认句'
        assert MP_VIEW in text, f'{relpath} 缺少小程序可查看提醒句'
        for forbidden in FORBIDDEN_COPY:
            assert forbidden not in text, f'{relpath} 出现禁止话术'


def test_miniprogram_build_message_has_hint_on_all_three_branches():
    js = _read('miniprogram/pages/template/index.js')
    assert js.count(WEB_CONFIRM) == 3
    assert js.count(MP_VIEW) == 3
    assert "trigger === 'cold'" in js
    assert "trigger === 'heat'" in js
    assert '【日常提醒】' in js


def test_pair_management_weather_fail_closed_sentence_unchanged():
    text = _read('templates/pair_management.html')
    assert PAIR_MANAGEMENT_FAIL_CLOSED in text
    assert '确认需在微信小程序完成' not in text
    assert '可在微信小程序完成确认' not in text


def test_action_checkin_post_forms_unchanged():
    text = _read('templates/action_checkin.html')
    assert 'method="POST"' in text
    assert "url_for('public.action_confirm')" in text
    assert "url_for('public.action_help')" in text
    assert WEB_CONFIRM not in text


def test_helpers_caregiver_message_appends_web_confirm_for_all_alert_kinds():
    from services.user._helpers import _build_caregiver_message

    pair = SimpleNamespace(short_code='31415926', location_query='都昌', community_code='都昌')
    weather = {'temperature_max': 36, 'temperature_min': 2}
    for alert_kind in ('heat', 'cold', None):
        message = _build_caregiver_message(
            pair,
            alert_kind=alert_kind,
            weather_data=weather,
            action_link='https://example.test/action',
        )
        assert WEB_CONFIRM in message
        assert MP_VIEW in message
        assert message.count(WEB_CONFIRM) == 1


def test_caregiver_waiting_and_wechat_messages_use_web_confirm(
    client,
    db_session,
    monkeypatch,
):
    user = _create_user(db_session, 'copy_caregiver_wait', 'caregiver')
    pair = _create_pair(db_session, user.id, short_code='14142135')
    _login_as(client, user.id)
    _patch_caregiver_location(monkeypatch)
    monkeypatch.setattr(
        'services.user.caregiver_service.get_weather_with_cache',
        lambda _location: (dict(MOCK_WEATHER), False),
    )

    dashboard = client.get('/caregiver')
    assert dashboard.status_code == 200
    dashboard_body = dashboard.get_data(as_text=True)
    assert WEB_CONFIRM in dashboard_body
    assert MP_VIEW in dashboard_body
    assert PAIR_MANAGEMENT_FAIL_CLOSED in dashboard_body

    wechat = client.get(f'/caregiver/wechat_template?short_code={pair.short_code}')
    assert wechat.status_code == 200
    wechat_body = wechat.get_data(as_text=True)
    assert WEB_CONFIRM in wechat_body
    assert MP_VIEW in wechat_body
    assert 'id="wechatMessage"' in wechat_body
    assert '今日热风险：极高' not in wechat_body
    for forbidden in FORBIDDEN_COPY:
        assert forbidden not in wechat_body


def test_caregiver_and_community_wechat_real_weather_include_web_confirm(
    client,
    db_session,
    monkeypatch,
):
    user = _create_user(db_session, 'copy_real_weather_admin', 'admin')
    pair = _create_pair(db_session, user.id, short_code='17320508')
    db_session.add(Community(name='都昌', population=800, elderly_ratio=0.35))
    db_session.commit()
    _login_as(client, user.id)
    _patch_caregiver_location(monkeypatch)
    monkeypatch.setattr(
        'services.user.caregiver_service.get_weather_with_cache',
        lambda _location: (dict(REAL_WEATHER), False),
    )
    monkeypatch.setattr(
        'services.user.community_service.get_weather_with_cache',
        lambda _location: (dict(REAL_WEATHER), False),
    )
    monkeypatch.setattr(
        'services.user.caregiver_service.get_consecutive_hot_days',
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        'services.user.community_service.get_consecutive_hot_days',
        lambda *_args, **_kwargs: 0,
    )

    dashboard = client.get('/caregiver')
    assert dashboard.status_code == 200
    dashboard_body = dashboard.get_data(as_text=True)
    assert WEB_CONFIRM in dashboard_body
    assert '复制提醒话术' in dashboard_body

    caregiver_wechat = client.get(
        f'/caregiver/wechat_template?short_code={pair.short_code}&community_code=都昌'
    )
    assert caregiver_wechat.status_code == 200
    caregiver_body = caregiver_wechat.get_data(as_text=True)
    assert WEB_CONFIRM in caregiver_body
    assert MP_VIEW in caregiver_body
    assert '今日热风险：极高' in caregiver_body
    assert '天气更新中' not in caregiver_body

    community_wechat = client.get('/community/都昌/wechat')
    assert community_wechat.status_code == 200
    community_body = community_wechat.get_data(as_text=True)
    assert WEB_CONFIRM in community_body
    assert MP_VIEW in community_body
    assert 'id="wechatMessage"' in community_body
    assert '今日热风险：极高' in community_body
    for forbidden in FORBIDDEN_COPY:
        assert forbidden not in caregiver_body
        assert forbidden not in community_body
