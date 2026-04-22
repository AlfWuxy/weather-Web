# -*- coding: utf-8 -*-
def test_community_risk_api_reuses_cached_result(authenticated_client, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache

    clear_local_community_risk_cache()
    app = authenticated_client.application
    app.config['COMMUNITY_RISK_CACHE_TTL_SECONDS'] = 600

    calls = {'risk': 0}

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            calls['risk'] += 1
            return {
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
        return ({'temperature': 30.0, 'humidity': 65, 'aqi': 45}, True)

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

    monkeypatch.setattr('services.api_service.get_weather_with_cache', lambda city: ({'temperature': 29.0, 'humidity': 60, 'aqi': 40}, True))
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
