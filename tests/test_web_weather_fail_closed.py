# -*- coding: utf-8 -*-
"""照护与社区 Web 链真实天气 fail-closed 回归测试。"""
import json

import pytest

from core.db_models import Community, DailyStatus, Pair, User
from core.security import hash_short_code
from core.time_utils import today_local, utcnow


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
    user.set_password('weather-guard-test-password')
    db_session.add(user)
    db_session.commit()
    return user


def _create_pair(db_session, user_id, short_code='31415926'):
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


def test_heat_weather_guard_rejects_mock_and_missing_critical_fields():
    """mock 与缺少任一热风险关键字段的天气都不可进入计算。"""
    from services.user.caregiver_service import _heat_weather_available as caregiver_ready
    from services.user.community_service import _heat_weather_available as community_ready

    assert caregiver_ready(REAL_WEATHER) is True
    assert community_ready(REAL_WEATHER) is True
    assert caregiver_ready(MOCK_WEATHER) is False
    assert community_ready(MOCK_WEATHER) is False

    for missing_field in ('temperature', 'temperature_max', 'temperature_min', 'humidity'):
        incomplete = dict(REAL_WEATHER)
        incomplete.pop(missing_field)
        assert caregiver_ready(incomplete) is False
        assert community_ready(incomplete) is False


def test_caregiver_dashboard_does_not_calculate_mock_weather(
    client,
    db_session,
    monkeypatch,
):
    """照护工作台遇到 mock 时只显示等待状态和中性行动链接说明。"""
    user = _create_user(db_session, 'caregiver_mock_guard', 'caregiver')
    _create_pair(db_session, user.id)
    _login_as(client, user.id)
    _patch_caregiver_location(monkeypatch)
    monkeypatch.setattr(
        'services.user.caregiver_service.get_weather_with_cache',
        lambda _location: (dict(MOCK_WEATHER), False),
    )
    monkeypatch.setattr(
        'services.user.caregiver_service.HeatActionService.calculate_heat_risk',
        lambda *_args, **_kwargs: pytest.fail('mock 天气不应进入热风险计算'),
    )

    response = client.get('/caregiver')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '天气更新中' in body
    assert '风险等级暂不显示' in body
    assert '仍可发送行动链接并记录确认结果' in body
    assert '复制行动链接说明' in body
    assert '热风险：极高' not in body
    assert '高温（39°C）' not in body
    assert DailyStatus.query.count() == 0


def test_caregiver_action_log_keeps_risk_null_when_weather_is_mock(
    client,
    db_session,
    monkeypatch,
):
    """照护行动仍可记录，mock 天气不能写入 DailyStatus.risk_level。"""
    user = _create_user(db_session, 'caregiver_action_guard', 'caregiver')
    pair = _create_pair(db_session, user.id, short_code='27182818')
    _login_as(client, user.id)
    monkeypatch.setattr(
        'services.user.caregiver_service.get_weather_with_cache',
        lambda _location: (dict(MOCK_WEATHER), False),
    )
    monkeypatch.setattr(
        'services.user.caregiver_service.get_consecutive_hot_days',
        lambda *_args, **_kwargs: pytest.fail('mock 天气不应读取连续高温天数'),
    )

    response = client.post(
        f'/caregiver/pair/{pair.id}/action-log',
        data={
            'csrf_token': 'test-csrf-token',
            'caregiver_actions': 'remind',
            'caregiver_note': '已电话确认',
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    status = DailyStatus.query.filter_by(
        pair_id=pair.id,
        status_date=today_local(),
    ).one()
    assert status.risk_level is None
    assert json.loads(status.caregiver_actions) == ['remind']
    assert status.caregiver_note == '已电话确认'


def test_community_pages_do_not_generate_mock_risk_messages(
    client,
    db_session,
    monkeypatch,
):
    """社区工作台、微信模板和传播包遇到 mock 时均停止风险文案。"""
    user = _create_user(db_session, 'community_mock_guard', 'community')
    db_session.add(Community(name='都昌', population=800, elderly_ratio=0.35))
    db_session.commit()
    _login_as(client, user.id)
    monkeypatch.setattr(
        'services.user.community_service.get_weather_with_cache',
        lambda _location: (dict(MOCK_WEATHER), False),
    )
    monkeypatch.setattr(
        'services.user.community_service.HeatActionService.calculate_heat_risk',
        lambda *_args, **_kwargs: pytest.fail('mock 天气不应进入社区热风险计算'),
    )

    dashboard = client.get('/community')
    wechat = client.get('/community/都昌/wechat')
    announce = client.get('/community/announce?community=都昌')

    assert dashboard.status_code == 200
    dashboard_body = dashboard.get_data(as_text=True)
    assert '天气更新中' in dashboard_body
    assert '风险等级和转发内容暂缓更新' in dashboard_body
    assert 'd-flex flex-wrap gap-2 community-card-actions' in dashboard_body
    assert 'id="groupMessage-1"' not in dashboard_body
    assert 'class="btn btn-outline-primary btn-sm copy-community"' not in dashboard_body

    assert wechat.status_code == 200
    wechat_body = wechat.get_data(as_text=True)
    assert '天气更新中' in wechat_body
    assert '可转发提醒暂缓更新' in wechat_body
    assert 'id="wechatMessage"' not in wechat_body
    assert '今日热风险：极高' not in wechat_body

    assert announce.status_code == 200
    announce_body = announce.get_data(as_text=True)
    assert '状态：天气更新中' in announce_body
    assert 'class="btn btn-primary mt-3 copy-message"' not in announce_body
    assert '今日热风险：极高' not in announce_body
    assert DailyStatus.query.count() == 0


def test_real_qweather_still_generates_caregiver_and_community_risk(
    client,
    db_session,
    monkeypatch,
):
    """字段完整的真实 QWeather 仍应走现有计算与风险文案链。"""
    user = _create_user(db_session, 'real_weather_admin', 'admin')
    _create_pair(db_session, user.id, short_code='16180339')
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

    caregiver = client.get('/caregiver')
    community = client.get('/community')

    assert caregiver.status_code == 200
    caregiver_body = caregiver.get_data(as_text=True)
    assert '热风险：极高' in caregiver_body
    assert '复制提醒话术' in caregiver_body
    assert '天气更新中' not in caregiver_body

    assert community.status_code == 200
    community_body = community.get_data(as_text=True)
    assert 'id="groupMessage-1"' in community_body
    assert '今日热风险：极高' in community_body
    assert '天气更新中' not in community_body
