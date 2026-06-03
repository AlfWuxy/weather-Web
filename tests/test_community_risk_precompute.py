# -*- coding: utf-8 -*-
def test_precompute_community_risk_builds_and_reuses_cache(app, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache
    from services.pipelines.precompute_community_risk import precompute_community_risk

    clear_local_community_risk_cache()
    app.config['COMMUNITY_RISK_CACHE_TTL_SECONDS'] = 1500

    calls = {'weather': 0, 'risk': 0}

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

    def fake_get_weather_with_cache(location):
        calls['weather'] += 1
        return ({'temperature': 31.0, 'humidity': 70, 'aqi': 60, 'data_source': 'QWeather', 'is_mock': False}, True)

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

    clear_local_community_risk_cache()


def test_precompute_community_risk_skips_mock_weather(app, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache
    from services.pipelines.precompute_community_risk import precompute_community_risk

    clear_local_community_risk_cache()

    calls = {'risk': 0}

    class FakeCommunityService:
        def generate_community_risk_map(self, weather_data, target_date=None, window_days=None, disease_filter=None):
            calls['risk'] += 1
            return {}

    monkeypatch.setattr(
        'services.pipelines.precompute_community_risk.get_weather_with_cache',
        lambda location: ({'temperature': 37.0, 'humidity': 70, 'aqi': 90, 'is_mock': True, 'data_source': 'Demo'}, False),
    )
    monkeypatch.setattr('services.pipelines.precompute_community_risk.get_community_service', lambda: FakeCommunityService())

    result = precompute_community_risk(
        app=app,
        locations=['都昌'],
        window_days_list=[30],
        disease_filters=['']
    )

    assert result['weather_skipped'] == 1
    assert result['combinations'] == 0
    assert calls['risk'] == 0

    clear_local_community_risk_cache()
