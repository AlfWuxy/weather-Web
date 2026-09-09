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


def test_empty_community_table_uses_canonical_exploratory_screening(
    app,
    db_session,
    monkeypatch,
):
    class FailingDLNM:
        def calculate_rr(self, *_args, **_kwargs):
            raise RuntimeError("simulated DLNM failure")

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: FailingDLNM(),
    )
    service = CommunityRiskService()

    assert service.community_profiles == {}
    assert service.community_profile_status == {
        "available": False,
        "code": "community_table_empty",
        "source": "community_table",
        "message": "Community 表暂无社区档案，本次不生成社区风险排名。",
    }

    result = service.generate_community_risk_map({"temperature": 35})

    assert result["data_available"] is True
    assert result["ranking_mode"] == "exploratory_geospatial_screening"
    assert result["ranking_status"] == "available"
    assert len(result["map_data"]["features"]) == 16
    assert len(result["rankings"]) == 16
    assert result["summary"]["data_available"] is True
    assert result["summary"]["ranked_communities"] == 16
    assert result["summary"]["total_communities"] == 16
    assert result["management_suggestions"] == []
    assert result["macro_weather"]["available"] is False


def test_community_query_failure_keeps_independent_exploratory_screening(
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

    assert result["data_available"] is True
    assert result["ranking_mode"] == "exploratory_geospatial_screening"
    assert result["ranking_status"] == "available"
    assert len(result["map_data"]["features"]) == 16
    assert len(result["rankings"]) == 16
    assert result["summary"]["ranked_communities"] == 16
    assert result["summary"]["total_communities"] == 16


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


def test_incomplete_known_communities_use_exploratory_public_gis_screening(
    app,
    db_session,
    monkeypatch,
):
    from config import COMMUNITY_COORDS_GCJ
    from core.db_models import Community

    # ORM 目前只包含三项基础画像；四项完整风险字段仍会保持缺失。
    db_session.add_all([
        Community(
            name=name,
            population=100 + index,
            elderly_ratio=0.30,
            chronic_disease_ratio=0.10,
        )
        for index, name in enumerate(COMMUNITY_COORDS_GCJ)
    ])
    db_session.commit()
    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", COMMUNITY_COORDS_GCJ)

    class FailingDLNM:
        def calculate_rr(self, *_args, **_kwargs):
            raise RuntimeError("simulated DLNM failure")

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: FailingDLNM(),
    )

    service = CommunityRiskService()
    assert service.community_profile_status["code"] == "insufficient_vulnerability_data"

    def fail_if_medical_records_are_queried(*_args, **_kwargs):
        raise AssertionError("探索性公开 GIS 筛查不应读取病历窗口")

    monkeypatch.setattr(service, "_collect_medical_counts", fail_if_medical_records_are_queried)
    result = service.generate_community_risk_map({"temperature": 35})

    assert result["ranking_mode"] == "exploratory_geospatial_screening"
    assert result["ranking_status"] == "available"
    assert result["data_status"]["code"] == "exploratory_geospatial_screening"
    assert result["summary"]["ranking_mode"] == "exploratory_geospatial_screening"
    assert result["summary"]["ranked_communities"] == 16
    assert result["summary"]["unranked_communities"] == 0
    assert result["summary"]["ranking_unique_cells"] == 8
    assert result["summary"]["evidence_coverage_ratio"] == 1.0
    assert result["summary"]["matched_records"] is None
    assert result["summary"]["total_expected_excess"] is None
    assert result["macro_weather"]["used_in_ranking"] is False
    assert result["macro_weather"]["available"] is False
    assert result["impact_likelihood_matrix"]["data_available"] is False
    assert result["layers"]["risk_index"] == []
    assert result["layers"]["hotspot"] == []
    assert result["equity_stratification"]["quartiles"] == []
    assert result["equity_stratification"]["priority_communities"] == []
    assert result["management_suggestions"] == []
    assert len(result["map_data"]["features"]) == 16

    clinical_fields = (
        "risk_score",
        "risk_index",
        "weather_hazard_score",
        "vulnerability_index",
        "population",
        "elderly_ratio",
        "chronic_disease_ratio",
        "expected_excess_visits",
        "observed_cases",
        "expected_cases",
        "sir",
        "ci_low",
        "ci_high",
        "smoothed_sir",
        "probability_exceed_baseline",
        "uncertainty_index",
        "hotspot_category",
        "matrix_score",
    )
    for row in result["rankings"]:
        assert row["ranking_mode"] == "exploratory_geospatial_screening"
        assert row["ranking_eligible"] is True
        assert row["screening_score"] is not None
        assert row["screening_level"] in {"Q1", "Q2", "Q3", "Q4"}
        assert row["cell_id"].startswith("h28v06-")
        assert row["raw_values"]["q3_coverage_pct"] > 0
        assert row["coordinate_available"] is True
        assert row["risk_weights"] == {}
        assert row["risk_contributions"] == {}
        assert row["hazard_formula"] is None
        for field_name in clinical_fields:
            assert row[field_name] is None

    # 同一原生 MODIS 网格共享同一证据值，并保持并列名次。
    rows_by_cell = {}
    for row in result["rankings"]:
        rows_by_cell.setdefault(row["cell_id"], []).append(row)
    shared_cell_rows = next(rows for rows in rows_by_cell.values() if len(rows) > 1)
    assert len({row["screening_score"] for row in shared_cell_rows}) == 1
    assert len({row["rank"] for row in shared_cell_rows}) == 1
    assert all(row["is_tied"] is True for row in shared_cell_rows)

    omitted_fields = set(result["summary"]["omitted_fields"])
    assert {
        "population",
        "chronic_disease_ratio",
        "green_space_ratio",
        "heat_island_index",
        "medical_accessibility",
        "baseline_visits",
        "medical_records",
    } <= omitted_fields


def test_exploratory_screening_works_without_database_profiles_or_weather(
    app,
    db_session,
    monkeypatch,
):
    """规范坐标足以驱动公开筛查，不依赖 Community 行、天气或 DLNM。"""
    from config import COMMUNITY_COORDS_GCJ
    from core.db_models import Community

    db_session.query(Community).delete()
    db_session.commit()
    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", COMMUNITY_COORDS_GCJ)
    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: (_ for _ in ()).throw(AssertionError("纯 GIS 筛查不应加载 DLNM")),
    )

    service = CommunityRiskService()
    result = service.generate_exploratory_geospatial_screening()

    assert service.community_profiles == {}
    assert result["ranking_mode"] == "exploratory_geospatial_screening"
    assert result["ranking_status"] == "available"
    assert result["summary"]["ranked_communities"] == 16
    assert len(result["rankings"]) == 16
    assert result["macro_weather"] == {
        "available": False,
        "temperature": None,
        "rr": None,
        "lag_temperatures_used": 0,
        "used_in_ranking": False,
        "role": "unavailable",
    }
    assert result["ranking_metadata"]["weather_context_available"] is False


def test_exploratory_bundle_failure_keeps_screening_mode_and_reason(
    app,
    db_session,
    monkeypatch,
):
    """证据包失败必须显示真实原因，不能退回七字段误诊。"""
    from config import COMMUNITY_COORDS_GCJ
    from core.db_models import Community

    db_session.query(Community).delete()
    db_session.commit()
    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", COMMUNITY_COORDS_GCJ)
    monkeypatch.setattr(
        "services.community_vulnerability_evidence.build_exploratory_rankings",
        lambda names: {
            "status": "unavailable",
            "rankings": [],
            "metadata": {
                "requested_community_count": len(list(names)),
                "reason_code": "bundle_unreadable",
                "reason": "冻结证据包无法读取。",
                "excluded_communities": [],
                "omitted_fields": [],
            },
        },
    )

    result = CommunityRiskService().generate_exploratory_geospatial_screening()

    assert result["ranking_mode"] == "exploratory_geospatial_screening"
    assert result["ranking_status"] == "unavailable"
    assert result["rankings"] == []
    assert result["summary"]["ranked_communities"] == 0
    assert result["summary"]["total_communities"] == 16
    assert result["ranking_metadata"]["reason_code"] == "bundle_unreadable"
    assert "冻结证据包无法读取" in result["methodology"][0]
    assert all("完整性门" not in line for line in result["methodology"])


def test_partial_exploratory_screening_keeps_excluded_community_visible(
    app,
    db_session,
    monkeypatch,
):
    """部分证据失败时保留灰色未排名行及逐社区原因。"""
    from config import COMMUNITY_COORDS_GCJ
    from core.db_models import Community
    from services.community_vulnerability_evidence import build_exploratory_rankings

    names = list(COMMUNITY_COORDS_GCJ)[:2]
    db_session.query(Community).delete()
    db_session.commit()
    monkeypatch.setitem(
        app.config,
        "COMMUNITY_COORDS_GCJ",
        {name: COMMUNITY_COORDS_GCJ[name] for name in names},
    )
    partial = build_exploratory_rankings([names[0]])
    partial["status"] = "partial"
    partial["metadata"].update({
        "requested_community_count": 2,
        "excluded_community_count": 1,
        "excluded_communities": [{
            "community": names[1],
            "reason_code": "missing_required_evidence",
            "reason": "所在网格缺少必需证据。",
            "invalid_fields": ["q3_coverage_pct"],
        }],
    })
    monkeypatch.setattr(
        "services.community_vulnerability_evidence.build_exploratory_rankings",
        lambda _names: partial,
    )

    result = CommunityRiskService().generate_exploratory_geospatial_screening()

    assert result["ranking_status"] == "partial"
    assert result["summary"]["ranked_communities"] == 1
    assert result["summary"]["unranked_communities"] == 1
    assert len(result["rankings"]) == 2
    excluded = next(row for row in result["rankings"] if not row["ranking_eligible"])
    assert excluded["community"] == names[1]
    assert excluded["rank"] is None
    assert excluded["screening_score"] is None
    assert excluded["screening_color"] == "#94a3b8"
    assert excluded["invalid_fields"] == ["q3_coverage_pct"]
    assert "缺少必需证据" in excluded["data_message"]
    assert len(result["map_data"]["features"]) == 1


def test_ranking_input_signature_tracks_bundle_profiles_and_coordinates(
    app,
    db_session,
    monkeypatch,
):
    """证据包或社区坐标变化后不得继续命中旧排名缓存。"""
    from config import COMMUNITY_COORDS_GCJ
    from core.db_models import Community

    db_session.query(Community).delete()
    db_session.commit()
    coords = dict(COMMUNITY_COORDS_GCJ)
    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", coords)
    monkeypatch.setattr(
        "services.community_vulnerability_evidence.get_evidence_bundle_sha256",
        lambda: "bundle-a",
    )
    service = CommunityRiskService()
    signature_a = service.get_ranking_input_signature()

    first_name = next(iter(coords))
    changed_coords = dict(coords)
    changed_coords[first_name] = [coords[first_name][0] + 0.001, coords[first_name][1]]
    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", changed_coords)
    signature_coordinate_changed = service.get_ranking_input_signature()

    monkeypatch.setattr(
        "services.community_vulnerability_evidence.get_evidence_bundle_sha256",
        lambda: "bundle-b",
    )
    signature_bundle_changed = service.get_ranking_input_signature()

    assert len({signature_a, signature_coordinate_changed, signature_bundle_changed}) == 3
