# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone
import json

import pytest


def _static_screening_payload():
    """构造预计算路径可缓存的 16 行公开筛查结果。"""
    return {
        'ranking_mode': 'exploratory_geospatial_screening',
        'rankings': [
            {
                'community': f'静态筛查村{i + 1}',
                'rank': i + 1,
                'screening_score': round(100 - i, 1),
                'risk_index': None,
                'expected_excess_visits': None,
                'observed_cases': None,
            }
            for i in range(16)
        ],
        'summary': {
            'ranked_communities': 16,
            'historical_component_available': False,
        },
    }


def _trusted_weather(temperature=31.0, **overrides):
    payload = {
        'temperature': temperature,
        'temperature_max': temperature + 3,
        'temperature_min': temperature - 5,
        'humidity': 70,
        'pressure': 1005,
        'wind_speed': 1.8,
        'weather_condition': '晴',
        'aqi': 60,
        'data_source': 'QWeather',
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'quality_version': 1,
        'is_mock': False,
    }
    payload.update(overrides)
    return payload


def test_precompute_community_risk_builds_and_reuses_cache(app, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache
    from services.pipelines.precompute_community_risk import precompute_community_risk

    clear_local_community_risk_cache()
    app.config['COMMUNITY_RISK_CACHE_TTL_SECONDS'] = 1500

    calls = {'weather': 0, 'risk': 0, 'cache_only': []}

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            calls['risk'] += 1
            return {
                'map_data': {'ok': True},
                'rankings': [{'community_name': '甲村', 'risk_index': 55.0}],
                'summary': {'window_days': window_days},
                'macro_weather': {'temperature': weather_data.get('temperature')},
                'layers': {},
                'impact_likelihood_matrix': {},
                'equity_stratification': {},
                'methodology': [],
                'management_suggestions': [],
            }

    def fake_get_weather_with_cache(location, cache_only=False):
        calls['weather'] += 1
        calls['cache_only'].append(cache_only)
        return (_trusted_weather(), True)

    monkeypatch.setattr('services.pipelines.precompute_community_risk.get_weather_with_cache', fake_get_weather_with_cache)
    monkeypatch.setattr('services.pipelines.precompute_community_risk.get_community_service', lambda: FakeCommunityService())

    result1 = precompute_community_risk(
        app=app,
        locations=['都昌'],
        window_days_list=[30],
        disease_filters=['']
    )
    result2 = precompute_community_risk(
        app=app,
        locations=['都昌'],
        window_days_list=[30],
        disease_filters=['']
    )

    assert result1['combinations'] == 1
    assert result1['computed'] == 1
    assert result1['risk_cache_hits'] == 0
    assert result2['combinations'] == 1
    assert result2['computed'] == 0
    assert result2['risk_cache_hits'] == 1
    assert calls['risk'] == 1
    assert calls['weather'] == 2
    assert calls['cache_only'] == [True, True]

    clear_local_community_risk_cache()


def test_precompute_community_risk_builds_static_screening_for_mock_weather(
    app,
    monkeypatch,
):
    from services.community_risk_cache import clear_local_community_risk_cache
    from services.pipelines.precompute_community_risk import precompute_community_risk

    clear_local_community_risk_cache()

    calls = {'formal': 0, 'screening': 0, 'screening_payload': None}

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            del weather_data, target_date, window_days, disease_filter
            calls['formal'] += 1
            pytest.fail('mock 天气不得进入正式社区风险轨')

        def generate_exploratory_geospatial_screening(
            self,
            target_date=None,
            window_days=None,
            disease_filter=None,
        ):
            del target_date, window_days, disease_filter
            calls['screening'] += 1
            calls['screening_payload'] = _static_screening_payload()
            return calls['screening_payload']

    monkeypatch.setattr(
        'services.pipelines.precompute_community_risk.get_weather_with_cache',
        lambda location, cache_only=False: (
            {'temperature': 37.0, 'humidity': 70, 'aqi': 90, 'is_mock': True, 'data_source': 'Demo'},
            False,
        ),
    )
    monkeypatch.setattr('services.pipelines.precompute_community_risk.get_community_service', lambda: FakeCommunityService())

    result = precompute_community_risk(
        app=app,
        locations=['都昌'],
        window_days_list=[30],
        disease_filters=['']
    )

    assert result['weather_skipped'] == 1
    assert result['screening_only'] == 1
    assert result['combinations'] == 1
    assert result['computed'] == 1
    assert calls['formal'] == 0
    assert calls['screening'] == 1
    assert len(calls['screening_payload']['rankings']) == 16
    assert all(
        row['risk_index'] is None
        and row['expected_excess_visits'] is None
        and row['observed_cases'] is None
        for row in calls['screening_payload']['rankings']
    )

    clear_local_community_risk_cache()


def test_precompute_skips_expired_real_cache_without_fetcher(app, db_session, monkeypatch):
    from core.db_models import WeatherCache
    from core.time_utils import utcnow
    from services.community_risk_cache import clear_local_community_risk_cache
    from services.pipelines.precompute_community_risk import precompute_community_risk

    clear_local_community_risk_cache()
    app.config['DEMO_MODE'] = False
    app.config['WEATHER_CACHE_TTL_MINUTES'] = 10
    app.extensions['redis_client'] = None
    weather = {
        'temperature': 32.0,
        'humidity': 68,
        'aqi': 58,
        'data_source': 'QWeather',
        'is_mock': False,
    }
    db_session.add(WeatherCache(
        location='都昌县',
        fetched_at=utcnow() - timedelta(hours=2),
        payload=json.dumps(weather, ensure_ascii=False),
        is_mock=False,
    ))
    db_session.commit()

    calls = {'formal': 0, 'screening': 0, 'screening_payload': None}

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            del weather_data, target_date, window_days, disease_filter
            calls['formal'] += 1
            pytest.fail('过期天气不得进入正式社区风险计算')

        def generate_exploratory_geospatial_screening(
            self,
            target_date=None,
            window_days=None,
            disease_filter=None,
        ):
            del target_date, window_days, disease_filter
            calls['screening'] += 1
            calls['screening_payload'] = _static_screening_payload()
            return calls['screening_payload']

    monkeypatch.setattr(
        'core.weather.get_weather_fetcher',
        lambda: pytest.fail('cache-only 预计算不应访问天气 fetcher'),
    )
    monkeypatch.setattr(
        'services.pipelines.precompute_community_risk.get_community_service',
        lambda: FakeCommunityService(),
    )

    result = precompute_community_risk(
        app=app,
        locations=['都昌'],
        window_days_list=[30],
        disease_filters=[''],
    )

    assert result['weather_cache_hits'] == 0
    assert result['weather_skipped'] == 1
    assert result['screening_only'] == 1
    assert result['computed'] == 1
    assert result['combinations'] == 1
    assert calls['formal'] == 0
    assert calls['screening'] == 1
    assert len(calls['screening_payload']['rankings']) == 16

    clear_local_community_risk_cache()


def test_precompute_missing_cache_never_fetches_or_writes_fallback(app, db_session, monkeypatch):
    from core.db_models import WeatherCache
    from services.community_risk_cache import clear_local_community_risk_cache
    from services.pipelines.precompute_community_risk import precompute_community_risk

    clear_local_community_risk_cache()
    app.config['DEMO_MODE'] = False
    app.extensions['redis_client'] = None

    calls = {'formal': 0, 'screening': 0, 'screening_payload': None}

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            del weather_data, target_date, window_days, disease_filter
            calls['formal'] += 1
            pytest.fail('缺少真实天气缓存时不应计算正式社区风险')

        def generate_exploratory_geospatial_screening(
            self,
            target_date=None,
            window_days=None,
            disease_filter=None,
        ):
            del target_date, window_days, disease_filter
            calls['screening'] += 1
            calls['screening_payload'] = _static_screening_payload()
            return calls['screening_payload']

    monkeypatch.setattr(
        'core.weather.get_weather_fetcher',
        lambda: pytest.fail('cache-only 预计算不应访问天气 fetcher'),
    )
    monkeypatch.setattr(
        'services.pipelines.precompute_community_risk.get_community_service',
        lambda: FakeCommunityService(),
    )

    result = precompute_community_risk(
        app=app,
        locations=['都昌'],
        window_days_list=[30],
        disease_filters=[''],
    )

    assert result['weather_skipped'] == 1
    assert result['screening_only'] == 1
    assert result['combinations'] == 1
    assert result['computed'] == 1
    assert calls['formal'] == 0
    assert calls['screening'] == 1
    assert len(calls['screening_payload']['rankings']) == 16
    assert WeatherCache.query.count() == 0

    clear_local_community_risk_cache()
