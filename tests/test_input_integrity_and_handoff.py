# -*- coding: utf-8 -*-
"""输入完整性、地点边界与小程序行动交接回归。"""
from pathlib import Path

import pytest


def _login_as(client, user_id, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = f'{user_id}:1'
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def _create_user(db_session, username='input-integrity-user'):
    from core.db_models import User

    user = User(username=username, role='user', age=72, gender='男')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()
    return user


def _online_weather(temperature=31.4):
    return {
        'temperature': temperature,
        'humidity': 68,
        'data_source': 'QWeather',
        'is_mock': False,
        'is_demo': False,
    }


@pytest.mark.parametrize('raw', ['北京', '101010100', '116.20,29.27', '完全不存在地点'])
def test_strict_user_location_rejects_external_ids_coordinates_and_unknown(app, raw):
    from core.location_resolution import resolve_user_location

    with app.app_context():
        result = resolve_user_location(raw)

    assert result.valid is False
    assert result.raw == raw
    assert result.value is None
    assert '目前仅支持都昌县' in result.error


def test_strict_user_location_accepts_county_and_configured_village(app):
    from core.location_resolution import resolve_user_location

    with app.app_context():
        county = resolve_user_location('都昌')
        village = resolve_user_location('都昌县牛家垄周村')

    assert county.valid is True
    assert county.value == '都昌'
    assert village.valid is True
    assert village.value == '牛家垄周村'


def test_strict_user_location_rejects_external_default_city_misconfiguration(
    app,
    monkeypatch,
):
    from core.location_resolution import resolve_user_location

    with app.app_context():
        monkeypatch.setitem(app.config, 'DEFAULT_CITY', '北京')
        result = resolve_user_location('北京')

    assert result.valid is False


def test_tool_location_accepts_confirmed_dynamic_county_community_but_rejects_beijing(
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import Community

    user = _create_user(db_session, username='dynamic-county-community')
    db_session.add(Community(name='甲村', location='甲村'))
    db_session.commit()
    _login_as(client, user.id)
    monkeypatch.setattr(
        'blueprints.tools.get_qweather_forecast_with_cache',
        lambda location, days: ([], False, {'source': 'QWeather'}),
    )

    accepted = client.get('/forecast-7day?location=甲村')
    rejected = client.get('/forecast-7day?location=北京')

    assert accepted.status_code == 200
    assert 'value="甲村"' in accepted.get_data(as_text=True)
    assert rejected.status_code == 422
    assert '未找到这个地点' in rejected.get_data(as_text=True)


@pytest.mark.parametrize('path', ['/ml-prediction', '/forecast-7day'])
@pytest.mark.parametrize('invalid_location', ['火星一号社区', '北京'])
def test_invalid_tool_location_returns_422_preserves_input_and_skips_services(
    client,
    db_session,
    monkeypatch,
    path,
    invalid_location,
):
    user = _create_user(
        db_session,
        username=f"invalid-location-{path.rsplit('/', 1)[-1]}-{len(invalid_location)}",
    )
    _login_as(client, user.id)

    def unexpected(*_args, **_kwargs):
        raise AssertionError('非法地点不得调用天气或预测服务')

    monkeypatch.setattr('blueprints.tools.get_weather_with_cache', unexpected)
    monkeypatch.setattr('blueprints.tools.get_qweather_forecast_with_cache', unexpected)
    monkeypatch.setattr('blueprints.tools.get_ml_service', unexpected)
    monkeypatch.setattr('blueprints.tools.get_forecast_service', unexpected)

    if path == '/ml-prediction':
        response = client.post(
            path,
            data={
                'location': invalid_location,
                'age': '72',
                'csrf_token': 'test-csrf-token',
            },
        )
    else:
        response = client.get(f'{path}?location={invalid_location}')

    body = response.get_data(as_text=True)
    assert response.status_code == 422
    assert invalid_location in body
    assert '未找到这个地点' in body
    assert 'data-error-code="invalid_location"' in body


def test_ml_guest_gets_explicit_auth_requirement_without_weather_call(
    client,
    db_session,
    monkeypatch,
):
    del db_session
    client.get('/guest', follow_redirects=False)
    with client.session_transaction() as session:
        session['_csrf_token'] = 'test-csrf-token'

    def unexpected(*_args, **_kwargs):
        raise AssertionError('游客身份要求应在天气和模型调用前返回')

    monkeypatch.setattr('blueprints.tools.get_weather_with_cache', unexpected)
    monkeypatch.setattr('blueprints.tools.get_ml_service', unexpected)

    response = client.post(
        '/ml-prediction',
        data={
            'location': '都昌',
            'age': '72',
            'csrf_token': 'test-csrf-token',
        },
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 403
    assert '生成个人类别线索需要注册或登录正式账号' in body
    assert 'data-error-code="auth_required"' in body


def test_ml_service_error_is_classified_without_leaking_raw_error(
    client,
    db_session,
    monkeypatch,
):
    user = _create_user(db_session, username='ml-safe-error-user')
    _login_as(client, user.id)
    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda _location: (_online_weather(), False),
    )

    class FailedService:
        def predict_disease_risk(self, *_args, **_kwargs):
            return {'success': False, 'error': 'secret stack /srv/private/model.pkl'}

    monkeypatch.setattr('blueprints.tools.get_ml_service', lambda: FailedService())
    response = client.post(
        '/ml-prediction',
        data={
            'location': '都昌',
            'age': '72',
            'csrf_token': 'test-csrf-token',
        },
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '类别线索模型正在维护' in body
    assert 'secret stack' not in body
    assert '/srv/private' not in body


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('sbp', '59'),
        ('sbp', '261'),
        ('sbp', 'nan'),
        ('sbp', 'inf'),
        ('sbp', '1e2'),
        ('sbp', '9' * 64),
        ('fbg', '1.9'),
        ('fbg', '30.1'),
        ('fbg', 'nan'),
        ('fbg', 'inf'),
        ('fbg', '-inf'),
        ('fbg', '2e1'),
        ('fbg', '9' * 64),
    ],
)
def test_chronic_vital_rejection_is_422_and_stops_weather_and_risk(
    client,
    db_session,
    monkeypatch,
    field,
    value,
):
    user = _create_user(db_session, username=f'chronic-{field}-{len(value)}-{value[:2]}')
    _login_as(client, user.id)

    def unexpected(*_args, **_kwargs):
        raise AssertionError('无效生命体征不得调用天气或慢病风险服务')

    monkeypatch.setattr('blueprints.tools.get_weather_with_cache', unexpected)
    monkeypatch.setattr('blueprints.tools.get_chronic_service', unexpected)
    response = client.post(
        '/chronic-risk',
        data={
            'disease': 'hypertension',
            'sbp': value if field == 'sbp' else '',
            'fbg': value if field == 'fbg' else '',
            'csrf_token': 'test-csrf-token',
        },
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 422
    assert '请先修正生命体征输入' in body
    assert 'is-invalid' in body
    assert '综合风险评分' not in body


@pytest.mark.parametrize(
    ('sbp', 'fbg', 'expected_vitals'),
    [
        ('60', '2', {'sbp': 60.0, 'fbg': 2.0}),
        ('260', '30', {'sbp': 260.0, 'fbg': 30.0}),
        ('', '', {}),
    ],
)
def test_chronic_vital_boundaries_and_blank_values_are_accepted(
    client,
    db_session,
    monkeypatch,
    sbp,
    fbg,
    expected_vitals,
):
    user = _create_user(
        db_session,
        username=f'chronic-valid-{sbp or "blank"}-{fbg or "blank"}',
    )
    _login_as(client, user.id)
    monkeypatch.setattr(
        'blueprints.tools.get_weather_with_cache',
        lambda _location: (_online_weather(), False),
    )

    captured = {}

    class StubChronicService:
        def predict_individual_risk(self, profile, _weather):
            captured['vitals'] = profile['vitals']
            return {
                'overall_risk': {'score': 35, 'level': '低风险'},
                'disease_risks': {},
                'recommendations': [],
            }

    monkeypatch.setattr(
        'blueprints.tools.get_chronic_service',
        lambda: StubChronicService(),
    )
    response = client.post(
        '/chronic-risk',
        data={
            'disease': 'hypertension',
            'sbp': sbp,
            'fbg': fbg,
            'csrf_token': 'test-csrf-token',
        },
    )

    assert response.status_code == 200
    assert captured['vitals'] == expected_vitals


@pytest.mark.parametrize('answered_count', [0, 4])
def test_incomplete_health_assessment_returns_422_preserves_answers_and_skips_weather(
    authenticated_client,
    db_session,
    monkeypatch,
    answered_count,
):
    answers = [
        ('outdoor_exposure', 'low'),
        ('symptom_level', 'none'),
        ('hydration', 'good'),
        ('medication_adherence', 'good'),
        ('sleep_quality', 'good'),
    ]

    def unexpected(*_args, **_kwargs):
        raise AssertionError('未完成问卷不得读取天气或调用风险服务')

    monkeypatch.setattr(
        'services.user.profile_service.get_weather_with_cache',
        unexpected,
    )
    payload = dict(answers[:answered_count])
    payload['csrf_token'] = 'test-csrf-token'
    response = authenticated_client.post('/health-assessment', data=payload)

    body = response.get_data(as_text=True)
    assert response.status_code == 422
    assert f'还差 <span id="assessmentMissingCount">{5 - answered_count}</span> 项' in body
    assert 'assessment-question is-invalid' in body
    assert 'validateAssessment()' in body
    for name, value in answers[:answered_count]:
        assert f'name="{name}" value="{value}" class="visually-hidden assess-opt" checked' in body
    assert 'autofocus' in body
    assert db_session.query(__import__('core.db_models', fromlist=['HealthRiskAssessment']).HealthRiskAssessment).count() == 0


def test_formal_action_handoff_has_real_fallback_and_no_fake_code(app, client):
    app.config['WECHAT_FORMAL_RUNTIME'] = True
    app.config['WX_MINIPROGRAM_ACTION_CODE_IMAGE'] = ''

    response = client.get('/elder')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'data-testid="miniprogram-action-handoff"' in body
    assert 'data-miniprogram-path="pages/actions/index"' in body
    assert '搜索小程序“宜老平安”' in body
    assert 'data-testid="miniprogram-code-fallback"' in body
    assert '复制名称' in body
    assert '公共行动可直接在本机勾选' in body
    assert '从“照护”选择对应家人' in body
    assert '返回今日风险' in body
    assert '今天先做这 3 件事' in body
    assert 'alt="宜老平安官方小程序码' not in body


def test_miniprogram_code_image_contract_rejects_missing_static_and_accepts_https(app):
    from core.config import _validated_miniprogram_code_image

    assert _validated_miniprogram_code_image(
        'images/not-present-action-code.png',
        app.static_folder,
    ) == ''
    assert _validated_miniprogram_code_image(
        '../private/action-code.png',
        app.static_folder,
    ) == ''
    assert _validated_miniprogram_code_image(
        'https://cdn.example.org/yilao/action-code.png',
        app.static_folder,
    ) == 'https://cdn.example.org/yilao/action-code.png'
    assert _validated_miniprogram_code_image(
        'brand/yilao-avatar.png',
        app.static_folder,
    ) == 'brand/yilao-avatar.png'
    assert _validated_miniprogram_code_image(
        'HTTPS://cdn.example.org/yilao/action-code.png',
        app.static_folder,
    ) == 'https://cdn.example.org/yilao/action-code.png'


def test_formal_action_handoff_renders_normalized_remote_code_without_referrer(app, client):
    from core.config import _validated_miniprogram_code_image

    app.config['WECHAT_FORMAL_RUNTIME'] = True
    app.config['WX_MINIPROGRAM_ACTION_CODE_IMAGE'] = _validated_miniprogram_code_image(
        'HTTPS://cdn.example.org/yilao/action-code.png',
        app.static_folder,
    )

    response = client.get('/elder')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'src="https://cdn.example.org/yilao/action-code.png"' in body
    assert 'referrerpolicy="no-referrer"' in body
    assert 'data-testid="miniprogram-code-fallback"' not in body


def test_invalid_cooling_location_is_422_and_never_reads_weather(
    client,
    db_session,
    monkeypatch,
):
    del db_session
    def unexpected(*_args, **_kwargs):
        raise AssertionError('非法避暑地点不得读取天气快照或旧天气缓存')

    monkeypatch.setattr('services.public_service.get_bootstrap_payload', unexpected)
    monkeypatch.setattr('services.public_service.get_weather_with_cache', unexpected)
    response = client.get('/cooling?community=不存在的火星社区')
    body = response.get_data(as_text=True)

    assert response.status_code == 422
    assert 'value="不存在的火星社区"' in body
    assert '原输入已保留' in body
    assert 'data-error-code="invalid_location"' in body
    assert '都昌 · 当前室外' not in body


def test_cooling_uses_fresh_bootstrap_snapshot_and_renders_final_temperature_first(
    client,
    db_session,
    monkeypatch,
):
    del db_session
    monkeypatch.setattr(
        'services.public_service.get_bootstrap_payload',
        lambda: {
            'snapshot_id': 'snapshot-cooling-12345678',
            'fetched_at': '2026-08-12T20:30:00+08:00',
            'stale': False,
            'current_stale': False,
            'risk_stale': False,
            'current': _online_weather(31.4),
        },
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError('已有快照时不得混读旧天气缓存')

    monkeypatch.setattr('services.public_service.get_weather_with_cache', unexpected)
    response = client.get('/cooling?location=都昌')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'data-temp="31.4"' in body
    assert 'data-static-value="1"' in body
    assert '<span class="num">31.4</span>' in body
    assert '<span class="num">0</span>' not in body
    assert '天气快照 snapshot' in body


@pytest.mark.parametrize(
    'snapshot_overrides',
    [
        {'current_stale': True, 'risk_stale': False},
        {'current_stale': False, 'risk_stale': True},
        {'current_stale': False, 'risk_stale': False, 'current': {'is_mock': True}},
    ],
)
def test_cooling_existing_unusable_snapshot_never_shows_temperature_or_reads_legacy(
    client,
    db_session,
    monkeypatch,
    snapshot_overrides,
):
    del db_session
    snapshot = {
        'snapshot_id': 'snapshot-unusable-current',
        'stale': False,
        'current_stale': False,
        'risk_stale': False,
        'current': _online_weather(39.5),
    }
    snapshot.update(snapshot_overrides)
    monkeypatch.setattr(
        'services.public_service.get_bootstrap_payload',
        lambda: snapshot,
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError('已有快照时即使不可用也不得混读旧天气缓存')

    monkeypatch.setattr('services.public_service.get_weather_with_cache', unexpected)
    response = client.get('/cooling?location=都昌')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'data-fx="thermometer"' not in body
    assert '<span class="num">39.5</span>' not in body


def test_county_cooling_filter_keeps_all_configured_community_resources(
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import CoolingResource

    db_session.add(CoolingResource(
        community_code='牛家垄周村',
        name='周村已核验纳凉点',
        resource_type='社区服务场所',
        is_active=True,
    ))
    db_session.commit()
    monkeypatch.setattr(
        'services.public_service.get_bootstrap_payload',
        lambda: {
            'snapshot_id': 'snapshot-county-filter',
            'stale': True,
            'current': {},
        },
    )

    response = client.get('/cooling?community=都昌')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '周村已核验纳凉点' in body
    assert '<option value="都昌"></option>' in body
    assert '<option value="北京"></option>' not in body


def test_filtered_cooling_page_hides_unverified_candidate_preview(client, db_session):
    del db_session
    response = client.get('/cooling?community=牛家垄周村')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '待核验场所预览' not in body


def test_cooling_templates_explain_disabled_location_and_keep_server_value_static():
    project_root = Path(__file__).resolve().parents[1]
    template = (project_root / 'templates' / 'cooling.html').read_text(encoding='utf-8')
    script = (project_root / 'static' / 'js' / 'yilao-data-fx-extra.js').read_text(encoding='utf-8')

    assert '当前没有已核验地图点位，暂不能按位置查找' in template
    assert '地图服务尚未配置，暂不能读取位置' in template
    assert '开放时间未核验，请出发前确认' in template
    assert "el.dataset.staticValue !== '1'" in script
