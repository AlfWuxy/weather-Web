# -*- coding: utf-8 -*-

from services.community_risk_service import CommunityRiskService


def _build_service_with_fixed_profile():
    service = CommunityRiskService()
    service.community_profiles = {
        "测试社区": {
            "id": 1,
            "name": "测试社区",
            "location": "测试地点",
            "latitude": 29.35,
            "longitude": 116.37,
            "population": 100,
            "elderly_ratio": 0.4,
            "chronic_disease_ratio": 0.15,
            "green_space_ratio": 0.1,
            "heat_island_index": 0.5,
            "medical_accessibility": 0.6,
            "baseline_visits": 5.0,
        }
    }
    return service


def test_excess_risk_normalization_avoids_hard_saturation():
    service = _build_service_with_fixed_profile()

    elevated = service.calculate_community_risk_score("测试社区", weather_rr=2.42)
    calm = service.calculate_community_risk_score("测试社区", weather_rr=1.0)

    assert 0 < elevated["normalized_score"] < 100
    assert calm["normalized_score"] == 0.0
    assert elevated["expected_excess_visits"] > 0


def test_baseline_visits_estimation_scales_with_population():
    service = CommunityRiskService()

    small = service._estimate_baseline_visits(20)
    large = service._estimate_baseline_visits(200)

    assert large > small
    assert large == 6.0


def test_generate_map_passes_lag_temperatures_to_dlnm(monkeypatch):
    service = _build_service_with_fixed_profile()
    captured = {}

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            captured["temperature"] = temperature
            captured["lag_temperatures"] = lag_temperatures
            return 1.8, {}

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: StubDLNM()
    )

    result = service.generate_community_risk_map(
        {"temperature": 10, "lag_temperatures": [9, 8, 7]}
    )

    assert captured["temperature"] == 10.0
    assert captured["lag_temperatures"] == [10.0, 9.0, 8.0, 7.0]
    assert result["macro_weather"]["lag_temperatures_used"] == 4
