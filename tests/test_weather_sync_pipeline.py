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
