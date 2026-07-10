# -*- coding: utf-8 -*-

import math

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

    formula = elevated["hazard_formula"]
    assert formula["expression"] == (
        "Excess=max(WeatherRR-1,0)×VI×BaselineVisits; "
        "Hazard=clip((1-exp(-Excess/Efold))×100,0,100)"
    )
    assert set(formula) == {
        "expression",
        "weather_rr",
        "vi",
        "baseline_visits",
        "excess",
        "efold",
        "hazard",
    }

    recomputed_excess = (
        max(formula["weather_rr"] - 1.0, 0.0)
        * formula["vi"]
        * formula["baseline_visits"]
    )
    recomputed_hazard = min(
        100.0,
        max(0.0, (1.0 - math.exp(-recomputed_excess / formula["efold"])) * 100.0),
    )
    assert math.isclose(formula["excess"], recomputed_excess, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(formula["hazard"], recomputed_hazard, rel_tol=0, abs_tol=1e-12)
    assert elevated["normalized_score"] == round(formula["hazard"], 1)


def test_baseline_visits_estimation_scales_with_population():
    service = CommunityRiskService()

    small = service._estimate_baseline_visits(20)
    large = service._estimate_baseline_visits(200)

    assert large > small
    assert large == 6.0


def test_default_community_proxies_are_reproducible_across_instances():
    first_service = CommunityRiskService()
    second_service = CommunityRiskService()

    assert first_service.community_profiles == second_service.community_profiles
    assert first_service.community_profile_status["code"] == "offline_demo"


def test_default_community_proxies_are_stable_and_distinct():
    service = CommunityRiskService()
    first = service.community_profiles["牛家垄周村"]
    second = service.community_profiles["岭背徐村"]

    proxy_fields = (
        "latitude",
        "longitude",
        "green_space_ratio",
        "heat_island_index",
        "medical_accessibility",
    )
    assert tuple(first[field] for field in proxy_fields) != tuple(
        second[field] for field in proxy_fields
    )

    for profile in (first, second):
        assert 29.315 <= profile["latitude"] <= 29.385
        assert 116.335 <= profile["longitude"] <= 116.405
        assert 0.08 <= profile["green_space_ratio"] <= 0.12
        assert 0.45 <= profile["heat_island_index"] <= 0.55
        assert 0.55 <= profile["medical_accessibility"] <= 0.65


def test_empty_community_table_fails_closed_in_flask_app_context(app, db_session):
    service = CommunityRiskService()

    assert service.community_profiles == {}
    assert service.community_profile_status == {
        "available": False,
        "code": "community_table_empty",
        "source": "community_table",
        "message": "Community 表暂无社区档案，本次不生成社区风险排名。",
    }

    result = service.generate_community_risk_map({"temperature": 35})

    assert result["data_available"] is False
    assert result["data_status"]["code"] == "community_table_empty"
    assert result["map_data"]["features"] == []
    assert result["rankings"] == []
    assert result["summary"]["data_available"] is False
    assert result["summary"]["data_status"] == "community_table_empty"
    assert result["summary"]["total_communities"] == 0
    assert result["management_suggestions"] == []


def test_community_query_failure_fails_closed_in_flask_app_context(
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import Community

    query_type = type(Community.query)

    def fail_query(_query):
        raise RuntimeError("simulated Community query failure")

    monkeypatch.setattr(query_type, "all", fail_query)
    service = CommunityRiskService()

    assert service.community_profiles == {}
    assert service.community_profile_status["available"] is False
    assert service.community_profile_status["code"] == "community_query_failed"

    result = service.generate_community_risk_map({"temperature": 35})

    assert result["data_available"] is False
    assert result["data_status"]["code"] == "community_query_failed"
    assert result["map_data"]["features"] == []
    assert result["rankings"] == []
    assert result["summary"]["data_status"] == "community_query_failed"
    assert result["summary"]["total_communities"] == 0


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


def test_no_records_keep_historical_metrics_null_and_renormalize_weights(monkeypatch):
    service = _build_service_with_fixed_profile()

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            return 1.8, {}

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: StubDLNM(),
    )

    result = service.generate_community_risk_map({"temperature": 35})
    row = result["rankings"][0]

    assert result["summary"]["matched_records"] == 0
    assert result["summary"]["total_records"] == 0
    assert result["summary"]["historical_component_available"] is False
    assert result["summary"]["median_uncertainty_index"] is None

    for field in (
        "observed_cases",
        "expected_cases",
        "sir",
        "ci_low",
        "ci_high",
        "smoothed_sir",
        "probability_exceed_baseline",
        "burden_percentile",
        "uncertainty_index",
    ):
        assert row[field] is None

    assert row["historical_component_available"] is False
    assert row["uncertainty_penalty"] == 1.0
    assert row["risk_weights"] == {
        "weather": 0.5625,
        "svi": 0.4375,
        "burden": 0.0,
    }
    assert row["risk_contributions"]["burden"] == 0.0
    assert row["matrix_score"] is None
    assert row["hotspot_category"] == "数据不足"

    recomputed = (
        row["risk_weights"]["weather"] * row["weather_hazard_score"]
        + row["risk_weights"]["svi"] * row["svi_percentile"]
    )
    assert abs(recomputed - row["risk_index"]) <= 0.2
    assert abs(
        row["risk_contributions"]["weather"]
        - row["risk_weights"]["weather"] * row["weather_hazard_score"]
    ) <= 0.02
    assert abs(
        row["risk_contributions"]["svi"]
        - row["risk_weights"]["svi"] * row["svi_percentile"]
    ) <= 0.02
