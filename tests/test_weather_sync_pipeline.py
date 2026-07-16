# -*- coding: utf-8 -*-
"""天气兜底与行动同步 fail-closed 回归测试。"""

import json
from datetime import date

import pytest

from core.db_models import CommunityDaily, DailyStatus, Pair, User, WeatherData
from core.security import hash_short_code
from core.time_utils import utcnow


VALID_QWEATHER = {
    'temperature': 38.0,
    'temperature_max': 40.0,
    'temperature_min': 29.0,
    'humidity': 70.0,
    'pressure': 1002.0,
    'weather_condition': '晴',
    'wind_speed': 2.0,
    'pm25': 20,
    'aqi': 45,
    'is_mock': False,
    'data_source': 'QWeather',
}


class _Response:
    def __init__(self, status_code, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.text = 'upstream response'

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


class _PipelineWeatherService:
    def __init__(self, payload):
        self.payload = payload

    def get_current_weather(self, _location):
        return dict(self.payload) if isinstance(self.payload, dict) else self.payload

    def identify_extreme_weather(self, _weather_data):
        return {'is_extreme': False, 'conditions': []}


def _load_pipeline(app, monkeypatch):
    from services.pipelines import sync_weather_data as pipeline

    monkeypatch.setattr(pipeline, 'app', app)
    return pipeline


def _install_pipeline_weather(monkeypatch, pipeline, payload):
    monkeypatch.setattr(
        pipeline,
        'WeatherService',
        lambda: _PipelineWeatherService(payload),
    )


def _create_pair(db_session, owner, code, status='active', community='同步测试社区'):
    pair = Pair(
        caregiver_id=owner.id,
        community_code=community,
        elder_code=f'elder-{code}',
        short_code=code,
        short_code_hash=hash_short_code(code),
        status=status,
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.flush()
    return pair


@pytest.mark.parametrize(
    'response',
    [
        _Response(503, {'code': '503'}),
        _Response(200, json_error=ValueError('invalid json')),
        _Response(200, {'code': '401'}),
    ],
    ids=['http-error', 'invalid-json', 'business-error'],
)
def test_qweather_failures_try_openmeteo_before_mock(monkeypatch, response):
    """和风三类失败都必须先进入 Open-Meteo。"""
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_key = 'test-key'
    service.api_base_url = 'https://qweather.invalid'
    fallback = {
        'temperature': 31.0,
        'temperature_max': 34.0,
        'temperature_min': 25.0,
        'humidity': 65.0,
        'is_mock': False,
        'data_source': 'Open-Meteo',
    }
    fallback_calls = []

    monkeypatch.setattr(weather_module.requests, 'get', lambda *_args, **_kwargs: response)
    monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
    monkeypatch.setattr(
        service,
        '_get_openmeteo_weather',
        lambda city: fallback_calls.append(city) or fallback,
    )
    monkeypatch.setattr(
        service,
        '_get_mock_weather',
        lambda: pytest.fail('Open-Meteo 成功时不应进入 Mock'),
    )

    result = service.get_current_weather('都昌')

    assert result == fallback
    assert fallback_calls == ['都昌']


def test_qweather_and_openmeteo_failure_then_use_mock(monkeypatch):
    """两个真实 API 都失败后才允许返回 Mock。"""
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_key = 'test-key'
    service.api_base_url = 'https://qweather.invalid'
    mock_weather = {'is_mock': True, 'data_source': 'Mock'}

    monkeypatch.setattr(
        weather_module.requests,
        'get',
        lambda *_args, **_kwargs: _Response(502, {'code': '502'}),
    )
    monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
    monkeypatch.setattr(service, '_get_openmeteo_weather', lambda _city: None)
    monkeypatch.setattr(service, '_get_mock_weather', lambda: mock_weather)

    assert service.get_current_weather('都昌') == mock_weather


def test_qweather_budget_guard_blocks_http_and_uses_fallback(monkeypatch):
    """月度保护触发后不得再发和风请求。"""
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_key = 'test-key'
    service.api_base_url = 'https://qweather.invalid'
    fallback = {'is_mock': False, 'data_source': 'Open-Meteo'}

    monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: False)
    monkeypatch.setattr(
        weather_module.requests,
        'get',
        lambda *_args, **_kwargs: pytest.fail('预算保护后不应发送和风请求'),
    )
    monkeypatch.setattr(service, '_get_openmeteo_weather', lambda _city: fallback)

    assert service.get_current_weather('都昌') == fallback


def test_qweather_air_quality_v1_uses_origin_and_lat_lon(monkeypatch):
    """空气质量 v1 应去掉 /v7，并按纬度、经度顺序请求。"""
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_key = 'test-key'
    service.api_base_url = 'https://api.qweather.invalid/v7'
    service.canonical_location = '116.20,29.27'
    calls = []
    reserved_endpoints = []

    weather_response = _Response(200, {
        'code': '200',
        'now': {
            'temp': '30',
            'humidity': '70',
            'pressure': '1005',
            'text': '晴',
            'windSpeed': '3',
            'feelsLike': '33',
        },
    })
    air_response = _Response(200, {
        'indexes': [
            {'code': 'cn-mee-1h', 'aqi': 99, 'category': '良'},
            {'code': 'qaqi', 'aqi': 2.4, 'category': 'Good'},
            {'code': 'cn-mee', 'aqi': 83, 'category': '良'},
        ],
        'pollutants': [
            {'code': 'pm2p5', 'concentration': {'value': 27.5, 'unit': 'μg/m3'}},
        ],
    })

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return weather_response if len(calls) == 1 else air_response

    monkeypatch.setattr(weather_module.requests, 'get', fake_get)
    monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
    monkeypatch.setattr(
        weather_module,
        'reserve_qweather_request',
        lambda endpoint: reserved_endpoints.append(endpoint) or True,
    )
    monkeypatch.setattr(
        service,
        '_resolve_qweather_current_temperature_range',
        lambda _location: (34.0, 25.0, 'daily', 'high'),
    )

    result = service.get_current_weather('牛家垄周村')

    assert result['aqi'] == 83
    assert result['pm25'] == 27.5
    assert result['air_quality'] == '良'
    assert calls[0][0] == 'https://api.qweather.invalid/v7/weather/now'
    assert calls[1][0] == 'https://api.qweather.invalid/airquality/v1/current/29.27/116.20'
    assert calls[1][1]['params'] == {'lang': 'zh'}
    assert calls[1][1]['headers'] == {'X-QW-Api-Key': 'test-key'}
    assert reserved_endpoints == ['weather_now', 'airquality_v1_current']


def test_qweather_air_quality_failure_keeps_weather_available(monkeypatch):
    """空气质量失败时，已成功取得的天气实况仍应返回。"""
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_key = 'test-key'
    service.api_base_url = 'https://api.qweather.invalid/v7'
    service.canonical_location = '116.20,29.27'
    responses = iter([
        _Response(200, {
            'code': '200',
            'now': {'temp': '30', 'humidity': '70', 'text': '晴'},
        }),
        _Response(403, {}),
    ])

    monkeypatch.setattr(weather_module.requests, 'get', lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
    monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: True)
    monkeypatch.setattr(
        service,
        '_resolve_qweather_current_temperature_range',
        lambda _location: (34.0, 25.0, 'daily', 'high'),
    )
    monkeypatch.setattr(
        service,
        '_get_fallback_weather',
        lambda *_args: pytest.fail('空气质量失败不应让天气实况降级'),
    )

    result = service.get_current_weather('都昌')

    assert result['data_source'] == 'QWeather'
    assert result['temperature'] == 30.0
    assert result['aqi'] is None
    assert result['pm25'] is None


def test_qweather_air_quality_does_not_use_generic_qaqi(monkeypatch):
    """通用 QAQI 的量纲不得进入本地 0-500 AQI 健康阈值。"""
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_key = 'test-key'
    service.api_base_url = 'https://api.qweather.invalid/v7'
    response = _Response(200, {
        'indexes': [{'code': 'qaqi', 'aqi': 2.1, 'category': 'Good'}],
        'pollutants': [
            {'code': 'pm2p5', 'concentration': {'value': 18, 'unit': 'μg/m3'}},
        ],
    })

    monkeypatch.setattr(weather_module.requests, 'get', lambda *_args, **_kwargs: response)
    monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
    monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: True)

    result = service._get_qweather_air_quality('116.20,29.27')

    assert result == {'pm25': 18.0}


def test_qweather_air_quality_budget_guard_skips_http(monkeypatch):
    """空气质量预算被阻断后，不得继续发送 HTTP 请求。"""
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_key = 'test-key'
    service.api_base_url = 'https://api.qweather.invalid/v7'

    monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: False)
    monkeypatch.setattr(
        weather_module.requests,
        'get',
        lambda *_args, **_kwargs: pytest.fail('预算阻断后不应发送空气质量请求'),
    )

    assert service._get_qweather_air_quality('116.20,29.27') == {}


@pytest.mark.parametrize(
    'response',
    [
        _Response(200, json_error=ValueError('invalid json')),
        _Response(200, {'indexes': None, 'pollutants': None}),
        _Response(200, {
            'indexes': [{'code': 'cn-mee', 'aqi': float('inf'), 'category': ''}],
            'pollutants': [
                {'code': 'pm2p5', 'concentration': {'value': float('nan')}},
            ],
        }),
    ],
    ids=['invalid-json', 'empty-lists', 'non-finite-values'],
)
def test_qweather_air_quality_invalid_payload_stays_unknown(monkeypatch, response):
    """无效空气质量响应应保持未知，不能伪装为零值。"""
    from services import weather_service as weather_module

    service = weather_module.WeatherService()
    service.qweather_key = 'test-key'
    service.api_base_url = 'https://api.qweather.invalid/v7'

    monkeypatch.setattr(weather_module.requests, 'get', lambda *_args, **_kwargs: response)
    monkeypatch.setattr(weather_module, '_record_external_api_timing', lambda *_args: None)
    monkeypatch.setattr(weather_module, 'reserve_qweather_request', lambda _endpoint: True)

    assert service._get_qweather_air_quality('116.20,29.27') == {}


@pytest.mark.parametrize(
    ('payload', 'reason'),
    [
        ({**VALID_QWEATHER, 'is_mock': True, 'data_source': 'Mock'}, 'mock_weather'),
        ({**VALID_QWEATHER, 'data_source': 'Open-Meteo'}, 'untrusted_weather_source'),
        ({**VALID_QWEATHER, 'temperature_max': None}, 'incomplete_weather'),
    ],
    ids=['mock', 'openmeteo', 'incomplete-qweather'],
)
def test_sync_daily_weather_rejects_untrusted_action_inputs(
    app,
    db_session,
    monkeypatch,
    payload,
    reason,
):
    """日天气同步不能把兜底或不完整数据写成可信天气。"""
    pipeline = _load_pipeline(app, monkeypatch)
    _install_pipeline_weather(monkeypatch, pipeline, payload)
    target_date = date(2026, 7, 10)

    result = pipeline.sync_daily_weather(
        target_date=target_date,
        location='同步测试社区',
    )

    assert result['updated'] is False
    assert result['skipped'] is True
    assert result['reason'] == reason
    assert WeatherData.query.filter_by(
        date=target_date,
        location='同步测试社区',
    ).count() == 0


def test_sync_daily_weather_writes_complete_qweather(
    app,
    db_session,
    monkeypatch,
):
    """字段完整的真实和风天气仍能正常写入。"""
    pipeline = _load_pipeline(app, monkeypatch)
    _install_pipeline_weather(monkeypatch, pipeline, VALID_QWEATHER)
    target_date = date(2026, 7, 11)

    result = pipeline.sync_daily_weather(
        target_date=target_date,
        location='同步测试社区',
    )

    assert result['updated'] is True
    assert result['skipped'] is False
    assert result['weather_source'] == 'QWeather'
    record = WeatherData.query.filter_by(
        date=target_date,
        location='同步测试社区',
    ).one()
    assert record.temperature_max == 40.0
    assert record.temperature_min == 29.0


@pytest.mark.parametrize(
    ('payload', 'reason'),
    [
        ({**VALID_QWEATHER, 'is_mock': True, 'data_source': 'Mock'}, 'mock_weather'),
        ({**VALID_QWEATHER, 'data_source': 'Open-Meteo'}, 'untrusted_weather_source'),
        ({**VALID_QWEATHER, 'humidity': None}, 'incomplete_weather'),
    ],
    ids=['mock', 'openmeteo', 'incomplete-qweather'],
)
def test_sync_action_daily_skips_untrusted_weather_without_writes(
    app,
    db_session,
    monkeypatch,
    payload,
    reason,
):
    """行动同步遇到非可信天气时不得落天气、风险或社区聚合。"""
    owner = User(username=f'action-owner-{reason}', role='caregiver')
    owner.set_password('test-password')
    db_session.add(owner)
    db_session.flush()
    _create_pair(db_session, owner, f'91{len(reason):06d}')
    db_session.commit()

    pipeline = _load_pipeline(app, monkeypatch)
    _install_pipeline_weather(monkeypatch, pipeline, payload)
    monkeypatch.setattr(
        pipeline,
        'get_consecutive_hot_days',
        lambda *_args, **_kwargs: pytest.fail('非可信天气不应进入连续高温计算'),
    )
    target_date = date(2026, 7, 12)

    result = pipeline.sync_action_daily(target_date=target_date)

    skipped = result['skipped_communities']['同步测试社区']
    assert result['updated'] == 0
    assert result['processed_communities'] == 0
    assert result['reason'] == 'weather_unavailable_for_all_communities'
    assert skipped['reason'] == reason
    assert WeatherData.query.filter_by(date=target_date).count() == 0
    assert DailyStatus.query.filter_by(status_date=target_date).count() == 0
    assert CommunityDaily.query.filter_by(date=target_date).count() == 0


def test_sync_action_daily_aggregates_active_pairs_and_backup_escalation(
    app,
    db_session,
    monkeypatch,
):
    """聚合只统计 active Pair，并把 backup 计入升级链。"""
    owner = User(username='active-aggregation-owner', role='caregiver')
    owner.set_password('test-password')
    db_session.add(owner)
    db_session.flush()

    active_backup = _create_pair(db_session, owner, '92000001')
    active_caregiver = _create_pair(db_session, owner, '92000002')
    inactive_emergency = _create_pair(
        db_session,
        owner,
        '92000003',
        status='inactive',
    )
    target_date = date(2026, 7, 13)
    db_session.add_all([
        DailyStatus(
            pair_id=active_backup.id,
            status_date=target_date,
            community_code='同步测试社区',
            confirmed_at=utcnow(),
            help_flag=False,
            relay_stage='backup',
        ),
        DailyStatus(
            pair_id=active_caregiver.id,
            status_date=target_date,
            community_code='同步测试社区',
            confirmed_at=None,
            help_flag=True,
            relay_stage='caregiver',
        ),
        DailyStatus(
            pair_id=inactive_emergency.id,
            status_date=target_date,
            community_code='同步测试社区',
            risk_level='极高',
            confirmed_at=utcnow(),
            help_flag=True,
            relay_stage='emergency',
        ),
    ])
    db_session.commit()

    pipeline = _load_pipeline(app, monkeypatch)
    _install_pipeline_weather(monkeypatch, pipeline, VALID_QWEATHER)
    monkeypatch.setattr(pipeline, 'get_consecutive_hot_days', lambda *_args, **_kwargs: 3)

    result = pipeline.sync_action_daily(target_date=target_date)

    assert result['updated'] == 2
    assert result['processed_communities'] == 1
    assert result['skipped'] is False
    record = CommunityDaily.query.filter_by(
        community_code='同步测试社区',
        date=target_date,
    ).one()
    assert record.total_people == 2
    assert record.confirm_rate == 0.5
    assert record.escalation_rate == 0.5
    assert sum(json.loads(record.risk_distribution).values()) == 2
    assert record.outreach_summary == '已有1个家庭进入升级链，优先安排社区跟进。'
