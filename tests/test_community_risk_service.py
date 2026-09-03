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


def test_baseline_visits_estimation_does_not_floor_small_population():
    service = CommunityRiskService()
    service.min_baseline_visits = 0.01

    tiny = service._estimate_baseline_visits(5)
    ten = service._estimate_baseline_visits(10)

    assert tiny == 5 * service.baseline_visit_rate
    assert ten == 10 * service.baseline_visit_rate
    assert tiny < ten


def test_community_risk_score_rejects_invalid_rr_and_missing_baseline():
    service = _build_service_with_fixed_profile()

    assert "error" in service.calculate_community_risk_score("测试社区", weather_rr=None)
    assert "error" in service.calculate_community_risk_score("测试社区", weather_rr="bad")
    assert "error" in service.calculate_community_risk_score("测试社区", weather_rr=float("nan"))

    service.community_profiles["测试社区"]["baseline_visits"] = None
    assert "error" in service.calculate_community_risk_score("测试社区", weather_rr=1.8)

    service.community_profiles["测试社区"]["baseline_visits"] = 0
    assert "error" in service.calculate_community_risk_score("测试社区", weather_rr=1.8)


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
    )
    assert tuple(first[field] for field in proxy_fields) != tuple(
        second[field] for field in proxy_fields
    )

    for profile in (first, second):
        assert 29.315 <= profile["latitude"] <= 29.385
        assert 116.335 <= profile["longitude"] <= 116.405
        assert profile["green_space_ratio"] is None
        assert profile["heat_island_index"] is None
        assert profile["medical_accessibility"] is None
        assert profile["coords_estimated"] is True


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


def test_generate_map_does_not_shift_lags_when_a_day_is_missing(monkeypatch):
    service = _build_service_with_fixed_profile()
    captured = {}

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            captured["lag_temperatures"] = lag_temperatures
            return 1.8, {}

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: StubDLNM(),
    )

    result = service.generate_community_risk_map(
        {"temperature": 10, "lag_temperatures": [9, None, 7]}
    )

    assert captured["lag_temperatures"] is None
    assert result["macro_weather"]["lag_temperatures_used"] == 0


def test_generate_map_fails_closed_when_dlnm_rr_invalid(monkeypatch):
    service = _build_service_with_fixed_profile()

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            return None, {}

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: StubDLNM(),
    )

    result = service.generate_community_risk_map({"temperature": 35})

    assert result["data_available"] is False
    assert result["rankings"] == []
    assert result["data_status"]["code"] == "weather_rr_unavailable"
    assert result["macro_weather"]["rr"] is None


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


def test_missing_community_coordinates_are_marked_estimated(app, db_session, monkeypatch):
    from core.db_models import Community

    db_session.add(Community(
        name='无坐标村',
        population=100,
        elderly_ratio=0.4,
        chronic_disease_ratio=0.1,
    ))
    db_session.add(Community(
        name='有坐标村',
        population=80,
        elderly_ratio=0.3,
        chronic_disease_ratio=0.1,
        latitude=29.28,
        longitude=116.21,
    ))
    db_session.commit()

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            return 1.8, {}

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: StubDLNM(),
    )

    service = CommunityRiskService()
    assert service.community_profiles['无坐标村']['coords_estimated'] is True
    assert service.community_profiles['有坐标村']['coords_estimated'] is False

    result = service.generate_community_risk_map({"temperature": 35})
    by_name = {row['community']: row for row in result['rankings']}
    assert by_name['无坐标村']['coords_estimated'] is True
    assert by_name['有坐标村']['coords_estimated'] is False

    estimated_feature = next(
        feature for feature in result['map_data']['features']
        if feature['properties']['name'] == '无坐标村'
    )
    surveyed_feature = next(
        feature for feature in result['map_data']['features']
        if feature['properties']['name'] == '有坐标村'
    )
    assert estimated_feature['properties']['coords_estimated'] is True
    assert surveyed_feature['properties']['coords_estimated'] is False
    assert any('估算坐标' in item for item in result['methodology'])


def test_missing_community_population_is_not_filled_with_100(app, db_session, monkeypatch):
    from core.db_models import Community

    db_session.add(Community(
        name='缺人口村',
        elderly_ratio=0.4,
        chronic_disease_ratio=0.1,
        latitude=29.28,
        longitude=116.21,
    ))
    db_session.add(Community(
        name='有人口村',
        population=80,
        elderly_ratio=0.3,
        chronic_disease_ratio=0.1,
        latitude=29.29,
        longitude=116.22,
    ))
    db_session.commit()

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            return 1.8, {}

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: StubDLNM(),
    )

    service = CommunityRiskService()
    assert service.community_profiles['缺人口村']['population'] is None
    assert service.community_profiles['缺人口村']['population'] != 100
    assert service.community_profiles['有人口村']['population'] == 80

    result = service.generate_community_risk_map({"temperature": 35})
    names = [row['community'] for row in result['rankings']]
    assert '缺人口村' not in names
    assert '有人口村' in names


def test_unmeasured_environment_fields_are_not_scored(app, db_session, monkeypatch):
    from core.db_models import Community

    db_session.add(Community(
        name='甲村',
        population=100,
        elderly_ratio=0.4,
        chronic_disease_ratio=0.1,
        latitude=29.28,
        longitude=116.21,
    ))
    db_session.add(Community(
        name='乙村',
        population=100,
        elderly_ratio=0.4,
        chronic_disease_ratio=0.1,
        latitude=29.29,
        longitude=116.22,
    ))
    db_session.commit()

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            return 1.8, {}

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: StubDLNM(),
    )

    service = CommunityRiskService()
    profile = service.community_profiles['甲村']
    assert profile['green_space_ratio'] is None
    assert profile['heat_island_index'] is None
    assert profile['medical_accessibility'] is None

    vi_a = service.calculate_vulnerability_index(profile)
    vi_b = service.calculate_vulnerability_index(service.community_profiles['乙村'])
    assert vi_a['breakdown']['green_contribution'] == 0
    assert vi_a['breakdown']['heat_island_contribution'] == 0
    assert vi_a['breakdown']['medical_contribution'] == 0
    assert vi_a.get('environment_in_score') is False
    assert vi_a['vulnerability_index'] == vi_b['vulnerability_index']

    result = service.generate_community_risk_map({"temperature": 35})
    by_name = {row['community']: row for row in result['rankings']}
    assert by_name['甲村']['vulnerability_index'] == by_name['乙村']['vulnerability_index']
    assert by_name['甲村'].get('environment_in_score') is False
    assert by_name['甲村']['theme_scores'].get('exposure') is None
    assert by_name['甲村']['theme_scores'].get('adaptive_gap') is None
    assert any('绿地' in item and '不计入' in item for item in result['methodology'])


def test_missing_sir_does_not_invent_median_burden(app, db_session, monkeypatch):
    from datetime import datetime, timezone

    from core.db_models import Community, MedicalRecord

    db_session.add_all([
        Community(
            name='甲村',
            population=120,
            elderly_ratio=0.33,
            chronic_disease_ratio=0.12,
            latitude=29.35,
            longitude=116.37,
        ),
        Community(
            name='乙村',
            population=80,
            elderly_ratio=0.41,
            chronic_disease_ratio=0.17,
            latitude=29.36,
            longitude=116.38,
        ),
    ])
    db_session.add(MedicalRecord(
        patient_name='甲村-样本',
        visit_time=datetime.now(timezone.utc),
        disease_category='呼吸系统',
        community='甲村',
    ))
    db_session.commit()

    original_rr = CommunityRiskService._rr_with_ci

    def fake_rr(self, observed, expected):
        if int(observed or 0) == 0:
            return None, None, None
        return original_rr(self, observed, expected)

    monkeypatch.setattr(CommunityRiskService, '_rr_with_ci', fake_rr)

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            return 1.8, {}

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: StubDLNM(),
    )

    result = CommunityRiskService().generate_community_risk_map({'temperature': 35})
    by_name = {row['community']: row for row in result['rankings']}

    assert result['summary']['historical_component_available'] is True
    assert by_name['甲村']['sir'] is not None
    assert by_name['甲村']['risk_weights']['burden'] == 0.20
    assert by_name['乙村']['sir'] is None
    assert by_name['乙村']['burden_percentile'] is None
    assert by_name['乙村']['burden_percentile'] != 50.0
    assert by_name['乙村']['risk_weights']['burden'] == 0.0
    assert by_name['乙村']['risk_contributions']['burden'] == 0.0
