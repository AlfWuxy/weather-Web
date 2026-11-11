# -*- coding: utf-8 -*-

import json
import math

from services.community_risk_service import (
    CommunityRiskService,
    _HAZARD_FORMULA_RECONSTRUCTABLE_FIELDS,
    _SUPPRESSED_HAZARD_FORMULA_NOTE,
)


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


_DEMOGRAPHIC_FIELDS = (
    "population",
    "elderly_ratio",
    "chronic_disease_ratio",
    "vulnerability_index",
)


def _demographic_profiles():
    def profile(name, population, raw_population, elderly_ratio, chronic_ratio):
        return {
            "id": name,
            "name": name,
            "location": name,
            "latitude": 29.35,
            "longitude": 116.37,
            "population": population,
            "elderly_ratio": elderly_ratio,
            "chronic_disease_ratio": chronic_ratio,
            "raw_population": raw_population,
            "raw_elderly_ratio": elderly_ratio,
            "raw_chronic_disease_ratio": chronic_ratio,
            "green_space_ratio": 0.1,
            "heat_island_index": 0.5,
            "medical_accessibility": 0.6,
            "baseline_visits": 5.0,
        }

    return {
        "小村": profile("小村", 4, 4, 0.5, 0.2),
        "大村": profile("大村", 80, 80, 0.3, 0.1),
        "外村": profile("外村", 90, 90, 0.22, 0.11),
        "缺人口": profile("缺人口", 100, None, 0.8, 0.25),
    }


def _service_with_demographic_profiles():
    service = CommunityRiskService()
    service.community_profiles = _demographic_profiles()
    return service


def _pin_ranking_threshold(monkeypatch):
    import services.community_risk_service as risk_module

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            return 1.5, {}

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: StubDLNM(),
    )
    monkeypatch.setattr(risk_module, "_community_stats_min_n", lambda: 10)
    monkeypatch.setattr(
        CommunityRiskService,
        "_load_community_profiles",
        lambda self: None,
    )
    monkeypatch.setattr(
        CommunityRiskService,
        "_collect_medical_counts",
        lambda self, *args, **kwargs: {
            "window_days": 30,
            "counts_by_community": {},
            "matched_records": 0,
            "total_records": 0,
            "unmatched_records": 0,
        },
    )


def _ranking_rows(result):
    return {row["community"]: row for row in result["rankings"]}


def _excess_from_hazard_fields(formula):
    try:
        hazard = float(formula["hazard"])
        efold = float(formula["efold"])
    except (TypeError, ValueError, KeyError):
        return None
    if efold == 0 or hazard <= 0 or hazard >= 100:
        return None
    return -efold * math.log(1.0 - hazard / 100.0)


def _is_real_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _vi_from_published_excess(excess, weather_rr, baseline):
    """Excess = max(RR-1, 0) × VI × Baseline。缺任一公开数时解不出来。"""
    if not _is_real_number(excess) or not _is_real_number(weather_rr) or not _is_real_number(baseline):
        return None
    scale = max(float(weather_rr) - 1.0, 0.0) * float(baseline)
    if scale == 0:
        return None
    return float(excess) / scale


def _vi_from_hazard_score(hazard, weather_rr, baseline, efold):
    """Hazard = clip((1-exp(-Excess/Efold))×100)。危险度可逆回 Excess，再回推 VI。"""
    if not _is_real_number(hazard) or not _is_real_number(efold):
        return None
    hazard = float(hazard)
    efold = float(efold)
    if efold <= 0 or hazard <= 0 or hazard >= 100:
        return None
    excess = -efold * math.log(1.0 - hazard / 100.0)
    return _vi_from_published_excess(excess, weather_rr, baseline)


def _reconstruct_vi_from_formula(formula):
    """用公式字段回推 VI。缺分量、或分量已置空时返回 None。"""
    if not isinstance(formula, dict):
        return None
    direct = formula.get("vi")
    if isinstance(direct, (int, float)) and not isinstance(direct, bool):
        return float(direct)
    excess = formula.get("excess")
    if not isinstance(excess, (int, float)) or isinstance(excess, bool):
        excess = _excess_from_hazard_fields(formula)
    try:
        weather_rr = float(formula["weather_rr"])
        baseline = float(formula["baseline_visits"])
        excess = float(excess)
    except (TypeError, ValueError, KeyError):
        return None
    scale = max(weather_rr - 1.0, 0.0) * baseline
    if scale == 0:
        return None
    return excess / scale


def _equity_stratum_for_svi(svi_percentile):
    svi = float(svi_percentile)
    if svi >= 75:
        return "Q4"
    if svi >= 50:
        return "Q3"
    if svi >= 25:
        return "Q2"
    return "Q1"


def _numeric(source, key):
    if not isinstance(source, dict):
        return None
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _svi_attack_estimates(row):
    """从 risk_contributions 与 svi 权重回推分位。缺任一项则没有可解方程。

    公开攻击是 (after_penalty - weather - burden) / weight。
    惩罚系数不是 1 时，先把 after_penalty 除回去。svi 或 before_penalty
    单独留下也同样能还原。
    """
    contributions = row.get("risk_contributions") if isinstance(row, dict) else None
    weights = row.get("risk_weights") if isinstance(row, dict) else None
    after = _numeric(contributions, "after_penalty")
    weather = _numeric(contributions, "weather")
    burden = _numeric(contributions, "burden")
    weight = _numeric(weights, "svi")
    if after is None or weather is None or burden is None or weight in (None, 0.0):
        solvable = []
    else:
        solvable = [(after - weather - burden) / weight]
        penalty = _numeric(row, "uncertainty_penalty")
        if penalty not in (None, 0.0, 1.0):
            solvable.append(((after / penalty) - weather - burden) / weight)
    if weight in (None, 0.0):
        return solvable
    before = _numeric(contributions, "before_penalty")
    if before is not None and weather is not None and burden is not None:
        solvable.append((before - weather - burden) / weight)
    svi_part = _numeric(contributions, "svi")
    if svi_part is not None:
        solvable.append(svi_part / weight)
    return solvable


def _recover_svi_from_decomposition(source):
    """(risk_index/uncertainty_penalty - w_weather*weather_hazard_score - w_burden*burden_percentile)/w_svi。

    任一输入不是实数时方程无解，返回 None。
    """
    if not isinstance(source, dict):
        return None
    weights = source.get("risk_weights")
    if not isinstance(weights, dict):
        return None
    risk_index = _numeric(source, "risk_index")
    penalty = _numeric(source, "uncertainty_penalty")
    weather = _numeric(source, "weather_hazard_score")
    burden = _numeric(source, "burden_percentile")
    weight_weather = _numeric(weights, "weather")
    weight_burden = _numeric(weights, "burden")
    weight_svi = _numeric(weights, "svi")
    if None in (
        risk_index,
        penalty,
        weather,
        burden,
        weight_weather,
        weight_burden,
        weight_svi,
    ):
        return None
    if penalty == 0 or weight_svi == 0:
        return None
    return (
        risk_index / penalty
        - weight_weather * weather
        - weight_burden * burden
    ) / weight_svi


def _first_number(*values):
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    return None


def _joined_decomposition(row, feature, summary_weights, extra_risk_indexes):
    """攻击者可以把排名行、地图、摘要权重和图层里的 risk_index 拼成同一条方程。"""
    weights = row.get("risk_weights") if isinstance(row.get("risk_weights"), dict) else None
    if weights is None and isinstance(feature, dict) and isinstance(feature.get("risk_weights"), dict):
        weights = feature.get("risk_weights")
    if weights is None and isinstance(summary_weights, dict):
        weights = summary_weights
    feature = feature or {}
    return {
        "risk_index": _first_number(
            row.get("risk_index"),
            feature.get("risk_index"),
            *extra_risk_indexes,
        ),
        "uncertainty_penalty": _first_number(
            row.get("uncertainty_penalty"),
            feature.get("uncertainty_penalty"),
        ),
        "weather_hazard_score": _first_number(
            row.get("weather_hazard_score"),
            feature.get("weather_hazard_score"),
        ),
        "burden_percentile": _first_number(
            row.get("burden_percentile"),
            feature.get("burden_percentile"),
        ),
        "risk_weights": weights,
    }


def _assert_svi_equation_misses(source, truth_svi, tolerance=0.1):
    estimate = _recover_svi_from_decomposition(source)
    assert estimate is None or abs(estimate - float(truth_svi)) > tolerance


def _assert_svi_not_recovered(row, truth_svi, tolerance=0.1):
    estimates = _svi_attack_estimates(row)
    assert estimates == []
    assert all(abs(estimate - float(truth_svi)) > tolerance for estimate in estimates)


def _assert_formula_has_no_precise_values(formula):
    assert isinstance(formula, dict)
    assert formula.get("expression") == _SUPPRESSED_HAZARD_FORMULA_NOTE
    for field in _HAZARD_FORMULA_RECONSTRUCTABLE_FIELDS:
        assert formula.get(field) is None
    assert _reconstruct_vi_from_formula(formula) is None
    assert not any(ch.isdigit() for ch in json.dumps(formula, ensure_ascii=False))


def _assert_demographics_hidden(row):
    for field in _DEMOGRAPHIC_FIELDS:
        assert row[field] is None
    assert row["suppressed"] is True
    _assert_formula_has_no_precise_values(row["hazard_formula"])
    assert row["theme_scores"] is None
    assert row["svi_percentile"] is None
    assert row.get("equity_stratum") is None
    assert row.get("risk_contributions") == {}
    assert _svi_attack_estimates(row) == []
    assert row["expected_excess_visits"] is None
    assert row["risk_score"] is None
    assert row["weather_hazard_score"] is None
    assert row["relative_index"] is None
    assert row["risk_index"] is None
    assert row["burden_percentile"] is None
    assert row["uncertainty_penalty"] is None
    assert row["risk_weights"] is None
    assert _recover_svi_from_decomposition(row) is None
    assert _vi_from_published_excess(row["expected_excess_visits"], 1.5, 5.0) is None


def _assert_precise_demographics(row, service, name):
    profile = service.community_profiles[name]
    expected_vi = service.calculate_vulnerability_index(profile)["vulnerability_index"]
    assert row["population"] == profile["raw_population"]
    assert row["elderly_ratio"] == profile["raw_elderly_ratio"]
    assert row["chronic_disease_ratio"] == profile["raw_chronic_disease_ratio"]
    assert row["vulnerability_index"] == expected_vi
    assert row["suppressed"] is False
    formula = row["hazard_formula"]
    assert formula["vi"] == expected_vi
    assert formula["expression"].startswith("Excess=max(WeatherRR-1,0)")
    recovered = _reconstruct_vi_from_formula(formula)
    assert recovered is not None
    assert math.isclose(recovered, expected_vi, rel_tol=0, abs_tol=1e-9)
    assert row["expected_excess_visits"] == round(formula["excess"], 1)
    assert row["weather_hazard_score"] == round(formula["hazard"], 1)
    assert row["risk_score"] == row["weather_hazard_score"]
    assert isinstance(row["relative_index"], (int, float))
    excess_vi = _vi_from_published_excess(
        row["expected_excess_visits"],
        formula["weather_rr"],
        formula["baseline_visits"],
    )
    assert excess_vi is not None
    assert abs(excess_vi - expected_vi) <= 0.05
    assert isinstance(row["svi_percentile"], (int, float))
    assert isinstance(row["theme_scores"], dict)
    assert row["theme_scores"]
    contributions = row["risk_contributions"]
    assert isinstance(contributions["svi"], (int, float))
    assert isinstance(contributions["weather"], (int, float))
    assert isinstance(contributions["burden"], (int, float))
    assert isinstance(contributions["before_penalty"], (int, float))
    assert isinstance(contributions["after_penalty"], (int, float))
    weight = row["risk_weights"]["svi"]
    assert isinstance(weight, (int, float)) and weight != 0
    assert abs(contributions["svi"] / weight - row["svi_percentile"]) <= 0.1
    estimates = _svi_attack_estimates(row)
    assert estimates
    assert min(abs(estimate - row["svi_percentile"]) for estimate in estimates) <= 0.1
    assert row["equity_stratum"] == _equity_stratum_for_svi(row["svi_percentile"])


def _assert_vulnerability_outputs_follow_suppression(result):
    rows = _ranking_rows(result)
    vulnerability = {
        entry["community"]: entry["value"]
        for entry in result["layers"]["vulnerability"]
    }
    risk_layers = {
        entry["community"]: entry["value"]
        for entry in result["layers"]["risk_index"]
    }
    priority_items = {
        item["community"]: item
        for item in result["equity_stratification"]["priority_communities"]
    }
    features = {
        feature["properties"]["name"]: feature["properties"]
        for feature in result["map_data"]["features"]
    }
    advices = [item["advice"] for item in result["management_suggestions"]]
    for name, row in rows.items():
        feature_formula = features[name]["hazard_formula"]
        if row["suppressed"]:
            assert vulnerability[name] is None
            if name in priority_items:
                assert priority_items[name].get("svi_percentile") is None
                assert priority_items[name].get("equity_stratum") is None
            _assert_formula_has_no_precise_values(feature_formula)
            assert features[name].get("risk_contributions") == {}
            assert _svi_attack_estimates(features[name]) == []
            assert features[name]["risk_score"] is None
            assert features[name]["weather_hazard_score"] is None
            assert features[name]["relative_index"] is None
            assert features[name]["risk_index"] is None
            assert features[name]["burden_percentile"] is None
            assert features[name]["uncertainty_penalty"] is None
            assert features[name]["risk_weights"] is None
            assert features[name]["expected_excess_visits"] is None
            assert risk_layers[name] is None
            assert _recover_svi_from_decomposition(features[name]) is None
            if name in priority_items:
                assert priority_items[name].get("risk_index") is None
            for advice in advices:
                assert name not in advice or "老龄化程度高(" not in advice
        else:
            assert vulnerability[name] == row["svi_percentile"]
            if name in priority_items:
                assert priority_items[name].get("svi_percentile") == row["svi_percentile"]
                assert priority_items[name].get("equity_stratum") == row["equity_stratum"]
            assert feature_formula == row["hazard_formula"]
            assert _reconstruct_vi_from_formula(feature_formula) is not None
            assert features[name]["risk_score"] == row["risk_score"]
            assert features[name]["weather_hazard_score"] == row["weather_hazard_score"]
            assert features[name]["relative_index"] == row["relative_index"]
            assert features[name]["risk_index"] == row["risk_index"]
            assert features[name]["burden_percentile"] == row["burden_percentile"]
            assert features[name]["uncertainty_penalty"] == row["uncertainty_penalty"]
            assert features[name]["risk_weights"] == row["risk_weights"]
            assert risk_layers[name] == row["risk_index"]
            feature_estimates = _svi_attack_estimates(features[name])
            assert feature_estimates
            assert min(
                abs(estimate - row["svi_percentile"]) for estimate in feature_estimates
            ) <= 0.1


def test_public_or_unauthorized_ranking_hides_demographics(monkeypatch):
    from flask import Flask

    _pin_ranking_threshold(monkeypatch)
    service = _service_with_demographic_profiles()
    app = Flask("ranking-demographic-public")

    with app.test_request_context():
        result = service.generate_community_risk_map({"temperature": 30})

    rows = _ranking_rows(result)
    assert set(rows) == {"小村", "大村", "外村", "缺人口"}
    for row in rows.values():
        _assert_demographics_hidden(row)
    assert rows["大村"]["stats_redacted"] is True
    assert rows["大村"]["stats_suppressed"] is False
    assert result["summary"]["suppressed"] is True
    for feature in result["map_data"]["features"]:
        assert feature["properties"]["population"] is None
        assert feature["properties"]["elderly_ratio"] is None
        assert feature["properties"]["vi"] is None
    _assert_vulnerability_outputs_follow_suppression(result)


def test_authorized_ranking_keeps_only_adequate_samples(monkeypatch):
    from flask import Flask
    import services.community_risk_service as risk_module

    _pin_ranking_threshold(monkeypatch)
    monkeypatch.setattr(
        risk_module,
        "_can_view_community_health_stats",
        lambda name: name == "大村",
    )
    service = _service_with_demographic_profiles()
    app = Flask("ranking-demographic-authorized")

    with app.test_request_context():
        result = service.generate_community_risk_map({"temperature": 30})

    rows = _ranking_rows(result)
    _assert_precise_demographics(rows["大村"], service, "大村")
    _assert_demographics_hidden(rows["外村"])
    _assert_demographics_hidden(rows["小村"])
    _assert_demographics_hidden(rows["缺人口"])
    assert rows["外村"]["population"] != 90
    assert rows["小村"]["population"] != 4
    assert rows["缺人口"]["population"] != 100
    assert rows["缺人口"]["elderly_ratio"] != 0.8
    features = {
        feature["properties"]["name"]: feature["properties"]
        for feature in result["map_data"]["features"]
    }
    assert features["大村"]["population"] == 80
    assert features["大村"]["vi"] == rows["大村"]["vulnerability_index"]
    assert features["小村"]["population"] is None
    assert features["缺人口"]["vi"] is None
    _assert_vulnerability_outputs_follow_suppression(result)


def test_requestless_precompute_hides_small_samples_only(monkeypatch):
    _pin_ranking_threshold(monkeypatch)
    service = _service_with_demographic_profiles()

    result = service.generate_community_risk_map({"temperature": 30})
    rows = _ranking_rows(result)

    _assert_precise_demographics(rows["大村"], service, "大村")
    _assert_precise_demographics(rows["外村"], service, "外村")
    _assert_demographics_hidden(rows["小村"])
    _assert_demographics_hidden(rows["缺人口"])
    assert rows["大村"]["stats_redacted"] is False
    _assert_vulnerability_outputs_follow_suppression(result)


def test_suppressed_formula_cannot_reconstruct_vi(monkeypatch):
    from flask import Flask
    import services.community_risk_service as risk_module

    _pin_ranking_threshold(monkeypatch)
    monkeypatch.setattr(
        risk_module,
        "_can_view_community_health_stats",
        lambda name: name == "大村",
    )
    service = _service_with_demographic_profiles()
    service.community_profiles["大村"]["elderly_ratio"] = 0.9
    service.community_profiles["大村"]["raw_elderly_ratio"] = 0.9
    app = Flask("suppressed-formula-vi")

    with app.test_request_context():
        result = service.generate_community_risk_map({"temperature": 30})

    rows = _ranking_rows(result)
    _assert_precise_demographics(rows["大村"], service, "大村")
    authorized_vi = rows["大村"]["vulnerability_index"]
    assert math.isclose(
        _reconstruct_vi_from_formula(rows["大村"]["hazard_formula"]),
        authorized_vi,
        rel_tol=0,
        abs_tol=1e-9,
    )

    suppressed_markers = []
    for name in ("小村", "外村", "缺人口"):
        row = rows[name]
        _assert_demographics_hidden(row)
        profile = service.community_profiles[name]
        expected_vi = service.calculate_vulnerability_index(profile)["vulnerability_index"]
        assert _reconstruct_vi_from_formula(row["hazard_formula"]) is None
        suppressed_markers.append((name, profile, expected_vi))

    _assert_vulnerability_outputs_follow_suppression(result)

    advices = [item["advice"] for item in result["management_suggestions"]]
    assert any("大村 老龄化程度高(90%)" in advice for advice in advices)
    for advice in advices:
        for name, profile, expected_vi in suppressed_markers:
            assert f"{profile['elderly_ratio'] * 100:.0f}%" not in advice
            assert f"{profile['chronic_disease_ratio'] * 100:.0f}%" not in advice
            assert f"{expected_vi:.2f}" not in advice
            assert f"{expected_vi:.3f}" not in advice
            assert f"{name} 老龄化程度高(" not in advice
    assert "90%" in " ".join(advices)


def test_public_suppressed_response_cannot_recover_svi_percentile(monkeypatch):
    """公开 / suppressed 响应不能从 risk_contributions 还原 svi_percentile 到 0.1。

    对照：同一批社区在未抑制，以及授权且样本达标时，
    (after_penalty - weather - burden) / svi_weight 仍能还原到 0.1。
    """
    from flask import Flask
    import services.community_risk_service as risk_module

    _pin_ranking_threshold(monkeypatch)
    service = _service_with_demographic_profiles()
    original_hidden = risk_module._ranking_demographics_hidden
    original_publication = risk_module._case_count_publication
    monkeypatch.setattr(
        risk_module,
        "_ranking_demographics_hidden",
        lambda name, sample: False,
    )
    monkeypatch.setattr(
        risk_module,
        "_case_count_publication",
        lambda name, sample: {"hide": False, "suppressed": False, "redacted": False},
    )
    truth = service.generate_community_risk_map({"temperature": 30})
    truth_rows = _ranking_rows(truth)
    truth_values = [row["svi_percentile"] for row in truth_rows.values()]
    assert max(truth_values) - min(truth_values) > 0.1
    for row in truth_rows.values():
        assert row["uncertainty_penalty"] == 1.0
        estimates = _svi_attack_estimates(row)
        assert estimates
        assert min(abs(estimate - row["svi_percentile"]) for estimate in estimates) <= 0.1

    monkeypatch.setattr(
        risk_module,
        "_ranking_demographics_hidden",
        original_hidden,
    )
    monkeypatch.setattr(
        risk_module,
        "_case_count_publication",
        original_publication,
    )
    public_app = Flask("svi-contribution-public")
    with public_app.test_request_context():
        public = service.generate_community_risk_map({"temperature": 30})

    public_features = {
        feature["properties"]["name"]: feature["properties"]
        for feature in public["map_data"]["features"]
    }
    assert set(_ranking_rows(public)) == set(truth_rows)
    for name, row in _ranking_rows(public).items():
        _assert_svi_not_recovered(row, truth_rows[name]["svi_percentile"])
        _assert_svi_not_recovered(public_features[name], truth_rows[name]["svi_percentile"])
        assert row["equity_stratum"] is None
        assert row["svi_percentile"] is None

    monkeypatch.setattr(
        risk_module,
        "_can_view_community_health_stats",
        lambda name: name == "大村",
    )
    authorized_app = Flask("svi-contribution-authorized")
    with authorized_app.test_request_context():
        authorized = service.generate_community_risk_map({"temperature": 30})
    authorized_rows = _ranking_rows(authorized)
    authorized_features = {
        feature["properties"]["name"]: feature["properties"]
        for feature in authorized["map_data"]["features"]
    }
    large = authorized_rows["大村"]
    _assert_precise_demographics(large, service, "大村")
    assert large["svi_percentile"] == truth_rows["大村"]["svi_percentile"]
    assert min(
        abs(estimate - large["svi_percentile"])
        for estimate in _svi_attack_estimates(large)
    ) <= 0.1
    assert min(
        abs(estimate - large["svi_percentile"])
        for estimate in _svi_attack_estimates(authorized_features["大村"])
    ) <= 0.1
    for name in ("小村", "外村", "缺人口"):
        _assert_svi_not_recovered(
            authorized_rows[name],
            truth_rows[name]["svi_percentile"],
        )
        _assert_svi_not_recovered(
            authorized_features[name],
            truth_rows[name]["svi_percentile"],
        )
        assert authorized_rows[name]["equity_stratum"] is None


def _assert_decomposition_cannot_recover(row, feature, summary_weights, extra_risk_indexes, truth_svi):
    """用风险分解方程还原，包括把图层和分位均值拼回 risk_index。不能落到 0.1 以内。"""
    _assert_svi_equation_misses(row, truth_svi)
    _assert_svi_equation_misses(feature, truth_svi)
    joined = _joined_decomposition(row, feature, summary_weights, extra_risk_indexes)
    candidates = []
    for value in (joined.get("risk_index"), *extra_risk_indexes):
        number = _first_number(value)
        if number is None or number in candidates:
            continue
        candidates.append(number)
    if not candidates:
        _assert_svi_equation_misses(joined, truth_svi)
        return
    for risk_index in candidates:
        source = dict(joined)
        source["risk_index"] = risk_index
        _assert_svi_equation_misses(source, truth_svi)


def _historical_case_counts(self, *args, **kwargs):
    return {
        "window_days": 30,
        "counts_by_community": {"小村": 1, "大村": 20, "外村": 8, "缺人口": 3},
        "matched_records": 32,
        "total_records": 32,
        "unmatched_records": 0,
    }


def _extra_risk_indexes(result, name):
    values = []
    for entry in result["layers"]["risk_index"]:
        if entry.get("community") == name:
            values.append(entry.get("value"))
    for item in result["equity_stratification"]["priority_communities"]:
        if item.get("community") == name:
            values.append(item.get("risk_index"))
    for quartile in result["equity_stratification"]["quartiles"]:
        values.append(quartile.get("avg_risk_index"))
    return values


def test_suppressed_risk_index_equation_cannot_recover_svi(monkeypatch):
    """抑制行不能用风险分解方程还原 svi_percentile 到 0.1。

    (risk_index/uncertainty_penalty - w_weather*weather_hazard_score
    - w_burden*burden_percentile) / w_svi
    在未抑制时能还原；未授权或小样本时，排名行、地图和可拼接的 risk_index 都解不出来。
    公开名单只按社区名排序。授权且样本达标的社区仍保留精确分解。
    """
    from flask import Flask
    import services.community_risk_service as risk_module

    _pin_ranking_threshold(monkeypatch)
    monkeypatch.setattr(
        CommunityRiskService,
        "_collect_medical_counts",
        _historical_case_counts,
    )
    service = _service_with_demographic_profiles()
    original_hidden = risk_module._ranking_demographics_hidden
    original_publication = risk_module._case_count_publication
    monkeypatch.setattr(
        risk_module,
        "_ranking_demographics_hidden",
        lambda name, sample: False,
    )
    monkeypatch.setattr(
        risk_module,
        "_case_count_publication",
        lambda name, sample: {"hide": False, "suppressed": False, "redacted": False},
    )
    truth = service.generate_community_risk_map({"temperature": 30})
    truth_rows = _ranking_rows(truth)
    truth_svis = [row["svi_percentile"] for row in truth_rows.values()]
    assert max(truth_svis) - min(truth_svis) > 0.1
    assert any(row["uncertainty_penalty"] != 1.0 for row in truth_rows.values())
    assert any(row["burden_percentile"] not in (None, 0, 0.0) for row in truth_rows.values())
    for row in truth_rows.values():
        estimate = _recover_svi_from_decomposition(row)
        assert estimate is not None
        assert abs(estimate - row["svi_percentile"]) <= 0.1
    truth_risk_order = [
        row["community"]
        for row in sorted(truth_rows.values(), key=lambda item: (-item["risk_index"], item["community"]))
    ]

    monkeypatch.setattr(risk_module, "_ranking_demographics_hidden", original_hidden)
    monkeypatch.setattr(risk_module, "_case_count_publication", original_publication)

    public_app = Flask("svi-equation-public")
    with public_app.test_request_context():
        public = service.generate_community_risk_map({"temperature": 30})

    public_names = [row["community"] for row in public["rankings"]]
    assert public_names == sorted(public_names)
    assert public_names != truth_risk_order
    assert all(row["percentile_rank"] is None for row in public["rankings"])
    assert all(
        feature["properties"]["percentile_rank"] is None
        for feature in public["map_data"]["features"]
    )
    assert [row["rank"] for row in public["rankings"]] == list(range(1, len(public_names) + 1))
    assert [feature["properties"]["name"] for feature in public["map_data"]["features"]] == public_names
    assert [entry["community"] for entry in public["layers"]["risk_index"]] == public_names
    public_features = {
        feature["properties"]["name"]: feature["properties"]
        for feature in public["map_data"]["features"]
    }
    for name, row in _ranking_rows(public).items():
        _assert_demographics_hidden(row)
        _assert_decomposition_cannot_recover(
            row,
            public_features[name],
            public["summary"]["risk_weights"],
            _extra_risk_indexes(public, name),
            truth_rows[name]["svi_percentile"],
        )

    monkeypatch.setattr(
        risk_module,
        "_can_view_community_health_stats",
        lambda name: name == "大村",
    )
    authorized_app = Flask("svi-equation-authorized")
    with authorized_app.test_request_context():
        authorized = service.generate_community_risk_map({"temperature": 30})
    authorized_rows = _ranking_rows(authorized)
    authorized_features = {
        feature["properties"]["name"]: feature["properties"]
        for feature in authorized["map_data"]["features"]
    }
    large = authorized_rows["大村"]
    _assert_precise_demographics(large, service, "大村")
    assert large["svi_percentile"] == truth_rows["大村"]["svi_percentile"]
    assert large["risk_index"] == truth_rows["大村"]["risk_index"]
    assert large["weather_hazard_score"] == truth_rows["大村"]["weather_hazard_score"]
    assert large["burden_percentile"] == truth_rows["大村"]["burden_percentile"]
    assert large["uncertainty_penalty"] == truth_rows["大村"]["uncertainty_penalty"]
    assert large["risk_weights"] == truth_rows["大村"]["risk_weights"]
    assert large["percentile_rank"] == truth_rows["大村"]["percentile_rank"]
    assert authorized_features["大村"]["percentile_rank"] == large["percentile_rank"]
    authorized_estimate = _recover_svi_from_decomposition(large)
    assert authorized_estimate is not None
    assert abs(authorized_estimate - large["svi_percentile"]) <= 0.1
    assert abs(_recover_svi_from_decomposition(authorized_features["大村"]) - large["svi_percentile"]) <= 0.1
    suppressed_names = [
        row["community"] for row in authorized["rankings"] if row["suppressed"]
    ]
    assert suppressed_names == sorted(suppressed_names)
    assert authorized["rankings"][0]["community"] == "大村"
    for name in ("小村", "外村", "缺人口"):
        _assert_demographics_hidden(authorized_rows[name])
        _assert_decomposition_cannot_recover(
            authorized_rows[name],
            authorized_features[name],
            authorized["summary"]["risk_weights"],
            _extra_risk_indexes(authorized, name),
            truth_rows[name]["svi_percentile"],
        )


def _fixed_baseline_profiles():
    """两个社区共用固定 baseline=5，VI 拉开，便于检验超额人次能否回推。"""

    def profile(name, population, elderly_ratio):
        return {
            "id": name,
            "name": name,
            "location": name,
            "latitude": 29.35,
            "longitude": 116.37,
            "population": population,
            "elderly_ratio": elderly_ratio,
            "chronic_disease_ratio": 0.1,
            "raw_population": population,
            "raw_elderly_ratio": elderly_ratio,
            "raw_chronic_disease_ratio": 0.1,
            "green_space_ratio": 0.1,
            "heat_island_index": 0.5,
            "medical_accessibility": 0.6,
            "baseline_visits": 5.0,
        }

    return {
        "大村": profile("大村", 80, 0.2),
        "小村": profile("小村", 40, 0.8),
    }


def _assert_suppressed_scores_hide_vi(row, weather_rr, baseline, true_vi, efold):
    assert row["suppressed"] is True
    assert row["expected_excess_visits"] is None
    assert row["risk_score"] is None
    assert row["weather_hazard_score"] is None
    assert row["relative_index"] is None
    assert _vi_from_published_excess(row["expected_excess_visits"], weather_rr, baseline) is None
    assert _vi_from_hazard_score(row["risk_score"], weather_rr, baseline, efold) is None
    assert _vi_from_hazard_score(row["weather_hazard_score"], weather_rr, baseline, efold) is None
    for estimate in (
        _vi_from_published_excess(row["expected_excess_visits"], weather_rr, baseline),
        _vi_from_hazard_score(row["risk_score"], weather_rr, baseline, efold),
        _vi_from_hazard_score(row["weather_hazard_score"], weather_rr, baseline, efold),
    ):
        assert estimate is None or abs(estimate - true_vi) > 0.05


def test_suppressed_excess_and_rr_cannot_reconstruct_vi(monkeypatch):
    """抑制行不能用超额人次、危险度或相对指数，配公开 RR 和固定 baseline 还原 VI。

    综合预警 top_risk 取的是 rankings[:3]，同一批行要一起置空。
    授权且样本达到 k 时，超额人次仍是公式的四舍五入精确值。
    """
    from flask import Flask
    import services.community_risk_service as risk_module

    _pin_ranking_threshold(monkeypatch)
    service = CommunityRiskService()
    service.community_profiles = _fixed_baseline_profiles()
    fixed_baseline = 5.0

    public_app = Flask("suppressed-excess-public")
    with public_app.test_request_context():
        public = service.generate_community_risk_map({"temperature": 30})

    public_rr = public["macro_weather"]["rr"]
    assert public_rr > 1
    public_rows = _ranking_rows(public)
    top_risk = public["rankings"][:3]
    assert [row["community"] for row in top_risk] == [row["community"] for row in public["rankings"]]
    assert public["summary"]["total_expected_excess"] is None
    for name, row in public_rows.items():
        true_vi = service.calculate_vulnerability_index(
            service.community_profiles[name]
        )["vulnerability_index"]
        _assert_suppressed_scores_hide_vi(
            row, public_rr, fixed_baseline, true_vi, service.excess_score_efold
        )
        leaked = service.calculate_community_risk_score(name, public_rr)["expected_excess_visits"]
        assert leaked > 0
        assert public["summary"]["total_expected_excess"] != leaked

    monkeypatch.setattr(
        risk_module,
        "_can_view_community_health_stats",
        lambda name: name == "大村",
    )
    authorized_app = Flask("suppressed-excess-authorized")
    with authorized_app.test_request_context():
        authorized = service.generate_community_risk_map({"temperature": 30})

    rows = _ranking_rows(authorized)
    macro_rr = authorized["macro_weather"]["rr"]
    assert macro_rr == public_rr
    features = {
        feature["properties"]["name"]: feature["properties"]
        for feature in authorized["map_data"]["features"]
    }
    authorized_row = rows["大村"]
    _assert_precise_demographics(authorized_row, service, "大村")
    true_authorized_vi = authorized_row["vulnerability_index"]
    recovered = _vi_from_published_excess(
        authorized_row["expected_excess_visits"],
        macro_rr,
        fixed_baseline,
    )
    assert recovered is not None
    assert abs(recovered - true_authorized_vi) <= 0.05
    hazard_vi = _vi_from_hazard_score(
        authorized_row["weather_hazard_score"],
        macro_rr,
        fixed_baseline,
        service.excess_score_efold,
    )
    assert hazard_vi is not None
    assert abs(hazard_vi - true_authorized_vi) <= 0.05
    assert authorized_row["relative_index"] == 100.0

    hidden = rows["小村"]
    true_hidden_vi = service.calculate_vulnerability_index(
        service.community_profiles["小村"]
    )["vulnerability_index"]
    assert abs(true_hidden_vi - true_authorized_vi) > 0.2
    _assert_suppressed_scores_hide_vi(
        hidden, macro_rr, fixed_baseline, true_hidden_vi, service.excess_score_efold
    )
    _assert_suppressed_scores_hide_vi(
        next(row for row in authorized["rankings"][:3] if row["community"] == "小村"),
        macro_rr,
        fixed_baseline,
        true_hidden_vi,
        service.excess_score_efold,
    )
    assert features["小村"]["risk_score"] is None
    assert features["小村"]["weather_hazard_score"] is None
    assert features["小村"]["relative_index"] is None
    assert features["大村"]["relative_index"] == authorized_row["relative_index"]

    formula = authorized_row["hazard_formula"]
    raw_authorized = formula["weather_rr"] * formula["vi"] * formula["baseline_visits"]
    published_mean = raw_authorized / (authorized_row["relative_index"] / 100.0)
    hidden_vi_from_mean = (2 * published_mean - raw_authorized) / (macro_rr * fixed_baseline)
    assert abs(hidden_vi_from_mean - true_hidden_vi) > 0.2

    hidden_excess = service.calculate_community_risk_score("小村", macro_rr)["expected_excess_visits"]
    assert hidden_excess > 0
    assert authorized["summary"]["total_expected_excess"] == authorized_row["expected_excess_visits"]
    assert authorized["summary"]["total_expected_excess"] != (
        authorized_row["expected_excess_visits"] + hidden_excess
    )
