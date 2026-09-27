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
    'pressure': 1002.0,
    'weather_condition': '晴',
    'wind_speed': 2.0,
    'aqi': 45,
    'pm25': 20,
    'air_quality_available': True,
    'data_source': 'QWeather',
    'observed_at': utcnow().isoformat(),
    'air_observed_at': utcnow().isoformat(),
    'quality_version': 1,
    'is_mock': False,
}


def _login_as(client, user_id, csrf_token='test-csrf-token'):
    from core.extensions import db

    user = db.session.get(User, user_id)
    assert user is not None
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def _session_flashes(client):
    with client.session_transaction() as session:
        return list(session.get('_flashes', ()))


def _create_user(db_session, username, role, community='都昌'):
    user = User(
        username=username,
        role=role,
        community=community,
        authorized_community=community if role != 'admin' else None,
    )
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
    """社区和照护基础热行动接收新鲜实况，同时继续拒绝 mock。"""
    from services.user.caregiver_service import _heat_weather_available as caregiver_ready
    from services.user.community_service import _heat_weather_available as community_ready

    assert caregiver_ready(REAL_WEATHER) is True
    assert community_ready(REAL_WEATHER) is True
    assert caregiver_ready(MOCK_WEATHER) is False
    assert community_ready(MOCK_WEATHER) is False

    openmeteo_weather = dict(REAL_WEATHER, data_source='Open-Meteo')
    assert caregiver_ready(openmeteo_weather) is True
    assert community_ready(openmeteo_weather) is True

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

    response = client.get('/caregiver', follow_redirects=True)

    assert response.status_code == 200
    assert response.request.path == '/pairs'
    body = response.get_data(as_text=True)
    assert '天气更新中' in body
    assert '风险等级暂不显示' in body
    assert '仍可发送行动链接并记录确认结果' in body
    assert '复制行动链接说明' in body
    assert '热风险：极高' not in body
    assert '高温（39°C）' not in body
    assert DailyStatus.query.count() == 0


def test_caregiver_dashboard_shows_openmeteo_locally_without_shareable_risk(
    client,
    db_session,
    monkeypatch,
):
    """Open-Meteo 可供本地基础行动，复制传播仍等待 fresh QWeather。"""
    user = _create_user(db_session, 'caregiver_openmeteo_guard', 'caregiver')
    _create_pair(db_session, user.id, short_code='14142135')
    _login_as(client, user.id)
    _patch_caregiver_location(monkeypatch)
    openmeteo_weather = dict(
        REAL_WEATHER,
        data_source='Open-Meteo',
        aqi=0,
        pm25=0,
        air_quality_available=False,
        aqi_estimated=True,
    )
    monkeypatch.setattr(
        'services.user.caregiver_service.get_weather_with_cache',
        lambda _location: (openmeteo_weather, False),
    )
    monkeypatch.setattr(
        'services.user.caregiver_service.get_consecutive_hot_days',
        lambda *_args, **_kwargs: 0,
    )

    response = client.get('/caregiver', follow_redirects=True)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '热风险：极高' in body
    assert '数据来源：Open-Meteo' in body
    assert '不代表官方预警' in body
    assert '高温行动阈值（39°C）' in body
    assert '<i class="bi bi-clipboard"></i> 复制行动链接说明' in body
    assert '<i class="bi bi-clipboard"></i> 复制提醒话术' not in body


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


@pytest.mark.parametrize(
    ('role', 'expected_path'),
    (
        ('caregiver', '/dashboard'),
        ('community', '/community'),
    ),
)
def test_community_announce_rejects_cross_community_for_non_admin_roles(
    client,
    db_session,
    role,
    expected_path,
):
    """caregiver 与 community 角色不可借 query 横向切社区。"""
    user = _create_user(db_session, f'{role}_announce_acl_guard', role)
    _login_as(client, user.id)

    response = client.get('/community/announce?community=南昌', follow_redirects=False)

    assert response.status_code == 302
    assert response.headers['Location'].endswith(expected_path)
    assert ('error', '无权访问该社区') in _session_flashes(client)


@pytest.mark.parametrize(
    ('role', 'community_name'),
    (
        ('caregiver', '都昌'),
        ('admin', '南昌'),
    ),
)
def test_community_announce_allows_authorized_scope(
    client,
    db_session,
    monkeypatch,
    role,
    community_name,
):
    """本社区 caregiver 与任意社区 admin 仍可生成 announce。"""
    user = _create_user(db_session, f'{role}_announce_allowed', role)
    _login_as(client, user.id)
    monkeypatch.setattr(
        'services.user.community_service.get_weather_with_cache',
        lambda _location: (dict(MOCK_WEATHER), False),
    )
    monkeypatch.setattr(
        'services.user.community_service.HeatActionService.calculate_heat_risk',
        lambda *_args, **_kwargs: pytest.fail('mock 天气不应进入社区热风险计算'),
    )

    response = client.get(f'/community/announce?community={community_name}')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '状态：天气更新中' in body
    assert community_name in body


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
    assert '微信群提醒待恢复' in wechat_body
    assert 'id="wechatMessage"' not in wechat_body
    assert '今日热风险：极高' not in wechat_body

    assert announce.status_code == 200
    announce_body = announce.get_data(as_text=True)
    assert '公共传播内容待恢复' in announce_body
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

    caregiver = client.get('/caregiver', follow_redirects=True)
    community = client.get('/community')

    assert caregiver.status_code == 200
    assert caregiver.request.path == '/pairs'
    caregiver_body = caregiver.get_data(as_text=True)
    assert '热风险：极高' in caregiver_body
    assert '复制提醒话术' in caregiver_body
    assert '天气更新中' not in caregiver_body

    assert community.status_code == 200
    community_body = community.get_data(as_text=True)
    assert 'id="groupMessage-1"' in community_body
    assert '今日热风险：极高' in community_body
    assert '天气更新中' not in community_body
