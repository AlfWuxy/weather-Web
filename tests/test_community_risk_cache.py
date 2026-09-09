# -*- coding: utf-8 -*-
from datetime import date

from core.time_utils import utcnow


def _fresh_qweather(**overrides):
    """生成满足社区风险生产门的和风实况夹具。"""
    payload = {
        'temperature': 30.0,
        'temperature_max': 34.0,
        'temperature_min': 25.0,
        'humidity': 65.0,
        'pressure': 1005.0,
        'wind_speed': 1.8,
        'weather_condition': '晴',
        'aqi': 45.0,
        'pm25': 20.0,
        'air_quality_available': True,
        'observed_at': utcnow().isoformat(),
        'air_observed_at': utcnow().isoformat(),
        'quality_version': 1,
        'data_source': 'QWeather',
        'is_mock': False,
    }
    payload.update(overrides)
    return payload


def test_community_risk_cache_namespace_is_v5():
    """输入指纹与无天气筛查上线后不得复用旧结果。"""
    from services.community_risk_cache import _build_cache_key

    cache_key = _build_cache_key({
        'analysis_date': '2025-10-30',
        'window_days': 30,
        'disease_filter': '',
        'city': '都昌',
        'weather': {'temperature': 30.0},
    })

    assert cache_key.startswith('community_risk:v5:')


def test_community_risk_cache_separates_ranking_path_and_input_signature():
    """天气轨道、社区画像或证据包变化时必须生成不同缓存键。"""
    from services.community_risk_cache import _build_cache_key

    base = {
        'analysis_date': '2025-10-30',
        'window_days': 30,
        'disease_filter': '',
        'city': '都昌',
        'weather': {},
    }
    auto_key = _build_cache_key({
        **base,
        'ranking_path': 'auto',
        'input_signature': 'bundle-a-profiles-a',
    })
    screening_key = _build_cache_key({
        **base,
        'ranking_path': 'exploratory_only',
        'input_signature': 'bundle-a-profiles-a',
    })
    changed_input_key = _build_cache_key({
        **base,
        'ranking_path': 'exploratory_only',
        'input_signature': 'bundle-b-profiles-a',
    })

    assert len({auto_key, screening_key, changed_input_key}) == 3


def test_none_result_is_not_written_to_any_cache(app, monkeypatch):
    """构建器没有生成可用结果时，不应写入本地或 Redis 缓存。"""
    import services.community_risk_cache as cache_module

    cache_module.clear_local_community_risk_cache()
    writes = []
    monkeypatch.setattr(cache_module, '_get_redis_client', lambda: None)
    monkeypatch.setattr(
        cache_module,
        '_set_local_cache',
        lambda *_args, **_kwargs: writes.append('local'),
    )
    monkeypatch.setattr(
        cache_module,
        '_redis_set_json',
        lambda *_args, **_kwargs: writes.append('redis'),
    )

    with app.app_context():
        result, cache_hit = cache_module.get_or_build_community_risk_result(
            {'analysis_date': '2025-10-30', 'ranking_path': 'auto'},
            lambda: None,
        )

    assert result is None
    assert cache_hit is False
    assert writes == []


def test_valid_weather_builder_none_returns_explicit_503(authenticated_client, monkeypatch):
    """正式天气通过后若社区分析未生成结果，接口仍须返回结构化错误。"""
    from services.community_risk_cache import clear_local_community_risk_cache

    class FakeCommunityService:
        def get_ranking_input_signature(self):
            return 'valid-weather-none-result'

        def generate_community_risk_map(self, *_args, **_kwargs):
            return None

    clear_local_community_risk_cache()
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda city: (_fresh_qweather(), True),
    )
    monkeypatch.setattr(
        'services.community_risk_service.get_community_service',
        lambda: FakeCommunityService(),
    )

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={'analysis_date': '2025-10-30', 'window_days': 30, 'city': '都昌'},
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )

    assert response.status_code == 503
    assert response.get_json() == {
        'success': False,
        'error': 'community_risk_unavailable',
        'message': '社区分析暂时不可用，请稍后再试。',
    }
    clear_local_community_risk_cache()


def test_community_risk_api_reuses_cached_result(authenticated_client, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache

    clear_local_community_risk_cache()
    app = authenticated_client.application
    app.config['COMMUNITY_RISK_CACHE_TTL_SECONDS'] = 600
    app.config['QWEATHER_CANONICAL_LOCATION'] = '116.20,29.27'

    calls = {'risk': 0, 'weather_locations': []}

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            calls['risk'] += 1
            return {
                'ranking_mode': 'exploratory_geospatial_screening',
                'ranking_status': 'available',
                'ranking_metadata': {'method_version': 'screening-test-v1'},
                'map_data': {'ok': True},
                'rankings': [{'community_name': '甲村', 'risk_index': 42.5}],
                'summary': {'window_days': window_days, 'weather_temperature': weather_data.get('temperature')},
                'macro_weather': {'temperature': weather_data.get('temperature')},
                'layers': {'risk_index': []},
                'impact_likelihood_matrix': {'impact_levels': [], 'likelihood_levels': []},
                'equity_stratification': {'quartiles': []},
                'methodology': ['cached-test'],
                'management_suggestions': ['keep-watch'],
            }

    def fake_get_weather_with_cache(city):
        return (_fresh_qweather(), True)

    monkeypatch.setattr('services.api_service.get_weather_with_cache', fake_get_weather_with_cache)
    monkeypatch.setattr('services.community_risk_service.get_community_service', lambda: FakeCommunityService())

    payload = {
        'analysis_date': '2025-10-30',
        'window_days': 30,
        'disease': '呼吸系统',
        'city': '都昌',
    }
    headers = {'X-CSRF-Token': 'test-csrf-token'}

    response1 = authenticated_client.post('/api/community/risk-map-v2', json=payload, headers=headers)
    response2 = authenticated_client.post('/api/community/risk-map-v2', json=payload, headers=headers)

    assert response1.status_code == 200
    assert response2.status_code == 200
    assert response1.get_json()['cache_hit'] is False
    assert response2.get_json()['cache_hit'] is True
    assert response1.get_json()['ranking_mode'] == 'exploratory_geospatial_screening'
    assert response1.get_json()['ranking_status'] == 'available'
    assert response1.get_json()['ranking_metadata']['method_version'] == 'screening-test-v1'
    assert calls['risk'] == 1

    clear_local_community_risk_cache()


def test_community_risk_api_recomputes_for_different_payload(authenticated_client, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache

    clear_local_community_risk_cache()

    calls = {'risk': 0}

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            calls['risk'] += 1
            return {
                'map_data': {},
                'rankings': [{'community_name': disease_filter or '全部', 'risk_index': 30.0}],
                'summary': {'window_days': window_days},
                'macro_weather': {},
                'layers': {},
                'impact_likelihood_matrix': {},
                'equity_stratification': {},
                'methodology': [],
                'management_suggestions': [],
            }

    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda city: (_fresh_qweather(temperature=29.0, humidity=60.0, aqi=40.0), True),
    )
    monkeypatch.setattr('services.community_risk_service.get_community_service', lambda: FakeCommunityService())

    headers = {'X-CSRF-Token': 'test-csrf-token'}
    payload_a = {'analysis_date': '2025-10-30', 'window_days': 30, 'disease': '呼吸系统', 'city': '都昌'}
    payload_b = {'analysis_date': '2025-10-30', 'window_days': 30, 'disease': '循环系统', 'city': '都昌'}

    response_a = authenticated_client.post('/api/community/risk-map-v2', json=payload_a, headers=headers)
    response_b = authenticated_client.post('/api/community/risk-map-v2', json=payload_b, headers=headers)

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert calls['risk'] == 2

    clear_local_community_risk_cache()


def test_precompute_cache_is_reused_by_risk_map_api(authenticated_client, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache
    from services.pipelines.precompute_community_risk import precompute_community_risk

    clear_local_community_risk_cache()
    app = authenticated_client.application
    app.config['COMMUNITY_RISK_CACHE_TTL_SECONDS'] = 600
    app.config['QWEATHER_CANONICAL_LOCATION'] = '116.20,29.27'

    calls = {'risk': 0, 'weather_locations': []}
    weather = _fresh_qweather(temperature=31.0, humidity=70.0, aqi=60.0)

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            calls['risk'] += 1
            return {
                'map_data': {'precomputed': True},
                'rankings': [],
                'summary': {'window_days': window_days},
                'macro_weather': {'temperature': weather_data.get('temperature')},
                'layers': {},
                'impact_likelihood_matrix': {},
                'equity_stratification': {},
                'methodology': [],
                'management_suggestions': [],
            }

    monkeypatch.setattr('services.pipelines.precompute_community_risk.get_weather_with_cache', lambda city: (weather, True))

    def fake_canonical_weather(city):
        calls['weather_locations'].append(city)
        return weather, True

    monkeypatch.setattr('services.api_service.get_weather_with_cache', fake_canonical_weather)
    monkeypatch.setattr('services.pipelines.precompute_community_risk.get_community_service', lambda: FakeCommunityService())
    monkeypatch.setattr('services.community_risk_service.get_community_service', lambda: FakeCommunityService())

    precompute_community_risk(
        app=app,
        locations=['都昌'],
        window_days_list=[30],
        disease_filters=['呼吸系统'],
        analysis_date=date(2025, 10, 30),
    )

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={'analysis_date': '2025-10-30', 'window_days': 30, 'disease': '呼吸系统', 'city': '都昌'},
        headers={'X-CSRF-Token': 'test-csrf-token'}
    )

    assert response.status_code == 200
    assert response.get_json()['cache_hit'] is True
    assert calls['risk'] == 1
    assert calls['weather_locations'] == ['116.2,29.27']

    clear_local_community_risk_cache()


def test_community_risk_api_recomputes_for_different_lag_temperatures(authenticated_client, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache

    clear_local_community_risk_cache()

    calls = {'risk': 0}

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            calls['risk'] += 1
            return {
                'map_data': {},
                'rankings': [],
                'summary': {'lag_temperatures': weather_data.get('lag_temperatures')},
                'macro_weather': {},
                'layers': {},
                'impact_likelihood_matrix': {},
                'equity_stratification': {},
                'methodology': [],
                'management_suggestions': [],
            }

    monkeypatch.setattr('services.community_risk_service.get_community_service', lambda: FakeCommunityService())
    headers = {'X-CSRF-Token': 'test-csrf-token'}
    base_payload = {
        'analysis_date': '2025-10-30',
        'window_days': 30,
        'disease': '呼吸系统',
        'city': '都昌',
    }

    response_a = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={**base_payload, 'weather': _fresh_qweather(
            temperature=30,
            humidity=60,
            aqi=40,
            lag_temperatures=[30, 29, 28],
        )},
        headers=headers
    )
    response_b = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={**base_payload, 'weather': _fresh_qweather(
            temperature=30,
            humidity=60,
            aqi=40,
            lag_temperatures=[30, 12, 10],
        )},
        headers=headers
    )

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert calls['risk'] == 2

    clear_local_community_risk_cache()


def test_community_risk_api_keeps_public_screening_when_weather_unavailable(
    authenticated_client,
    monkeypatch,
):
    """实时天气失败只关闭正式风险，冻结 GIS 筛查仍应返回并复用缓存。"""
    from services.community_risk_cache import clear_local_community_risk_cache

    clear_local_community_risk_cache()
    calls = {'screening': 0}

    class FakeCommunityService:
        def get_ranking_input_signature(self):
            return 'known-16-bundle-sha'

        def generate_exploratory_geospatial_screening(self, **_kwargs):
            calls['screening'] += 1
            return {
                'ranking_mode': 'exploratory_geospatial_screening',
                'ranking_status': 'available',
                'ranking_metadata': {
                    'method_version': 'screening-test-v1',
                    'weather_context_available': False,
                },
                'map_data': {'type': 'FeatureCollection', 'features': []},
                'rankings': [{'community': '甲村', 'screening_score': 55.0}],
                'summary': {'ranked_communities': 1, 'total_communities': 1},
                'macro_weather': {
                    'available': False,
                    'used_in_ranking': False,
                    'role': 'unavailable',
                },
                'layers': {'risk_index': []},
                'impact_likelihood_matrix': {'data_available': False},
                'equity_stratification': {'quartiles': []},
                'methodology': ['冻结公开 GIS 探索性筛查。'],
                'management_suggestions': [],
            }

    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda city: ({'temperature': 37, 'is_mock': True, 'data_source': 'Demo'}, False),
    )
    monkeypatch.setattr(
        'services.community_risk_service.get_community_service',
        lambda: FakeCommunityService(),
    )

    request_kwargs = {
        'json': {
            'analysis_date': '2025-10-30',
            'window_days': 30,
            'disease': '',
            'city': '都昌',
        },
        'headers': {'X-CSRF-Token': 'test-csrf-token'},
    }
    response1 = authenticated_client.post('/api/community/risk-map-v2', **request_kwargs)
    response2 = authenticated_client.post('/api/community/risk-map-v2', **request_kwargs)

    assert response1.status_code == 200
    assert response2.status_code == 200
    assert response1.get_json()['ranking_mode'] == 'exploratory_geospatial_screening'
    assert response1.get_json()['macro_weather']['available'] is False
    assert response2.get_json()['cache_hit'] is True
    assert calls['screening'] == 1
    clear_local_community_risk_cache()


def test_real_api_returns_known_16_screening_without_weather_or_community_rows(
    authenticated_client,
    db_session,
    monkeypatch,
):
    """真实 API 链在空 Community 表与无天气时仍返回 16 个规范社区。"""
    from config import COMMUNITY_COORDS_GCJ
    from core.db_models import Community
    import services.community_risk_service as risk_module
    from services.community_risk_cache import clear_local_community_risk_cache

    db_session.query(Community).delete()
    db_session.commit()
    monkeypatch.setattr(risk_module, '_community_service', None)
    monkeypatch.setitem(
        authenticated_client.application.config,
        'COMMUNITY_COORDS_GCJ',
        COMMUNITY_COORDS_GCJ,
    )
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda city: ({'is_mock': True, 'data_source': 'Demo'}, False),
    )
    clear_local_community_risk_cache()

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={'analysis_date': '2025-10-30', 'window_days': 30, 'city': '都昌'},
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ranking_mode'] == 'exploratory_geospatial_screening'
    assert payload['ranking_status'] == 'available'
    assert payload['summary']['ranked_communities'] == 16
    assert payload['summary']['ranking_unique_cells'] == 8
    assert len(payload['rankings']) == 16
    assert payload['macro_weather']['available'] is False
    assert all(row['risk_index'] is None for row in payload['rankings'])
    clear_local_community_risk_cache()


def test_community_risk_api_rejects_mock_weather_without_screening(
    authenticated_client,
    monkeypatch,
):
    """没有规范社区空间证据时继续拒绝 mock 天气风险请求。"""
    class FakeCommunityService:
        def get_ranking_input_signature(self):
            return 'no-screening-evidence'

        def generate_exploratory_geospatial_screening(self, **_kwargs):
            return None

    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda city: ({'temperature': 37, 'humidity': 70, 'aqi': 90, 'is_mock': True, 'data_source': 'Demo'}, False),
    )
    monkeypatch.setattr(
        'services.community_risk_service.get_community_service',
        lambda: FakeCommunityService(),
    )

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={'analysis_date': '2025-10-30', 'window_days': 30, 'disease': '', 'city': '都昌'},
        headers={'X-CSRF-Token': 'test-csrf-token'}
    )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload['error'] == 'weather_unavailable'
    assert payload['is_mock'] is True
