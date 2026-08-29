# -*- coding: utf-8 -*-
"""干净发布移植后的社区双轨后端回归测试。"""

import json
from copy import deepcopy
from datetime import datetime, timezone

from config import CITY_LOCATION_MAP, COMMUNITY_COORDS_GCJ
from services.community_risk_service import CommunityRiskService
from services.community_vulnerability_evidence import (
    DEFAULT_GEOJSON_PATH,
    EXPECTED_BUNDLE_SHA256,
    RANKING_METHOD_VERSION,
    SCREENING_BANDS,
    build_exploratory_rankings,
    get_evidence_bundle_sha256,
)


def _canonical_community_names():
    """只返回同时具有展示坐标和证据坐标的规范社区。"""
    return [name for name in COMMUNITY_COORDS_GCJ if name in CITY_LOCATION_MAP]


def _trusted_weather(temperature=30.0):
    """构造通过正式天气来源门的最小测试实况。"""
    return {
        "temperature": temperature,
        "temperature_max": temperature + 3,
        "temperature_min": temperature - 5,
        "humidity": 65,
        "pressure": 1005,
        "wind_speed": 1.8,
        "weather_condition": "晴",
        "aqi": 45,
        "data_source": "QWeather",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "quality_version": 1,
        "is_mock": False,
    }


def _minimal_result(mode=None):
    """构造 API 和预计算测试共用的最小结果。"""
    result = {
        "map_data": {"type": "FeatureCollection", "features": []},
        "rankings": [],
        "summary": {},
        "macro_weather": {},
        "layers": {},
        "impact_likelihood_matrix": {},
        "equity_stratification": {},
        "methodology": [],
        "management_suggestions": [],
    }
    if mode:
        result.update({
            "ranking_mode": mode,
            "ranking_status": "available",
            "ranking_metadata": {"method_version": RANKING_METHOD_VERSION},
        })
    return result


def test_frozen_bundle_ranks_all_canonical_communities_with_pinned_sha():
    names = _canonical_community_names()
    result = build_exploratory_rankings(names)

    assert len(names) == 16
    assert result["status"] == "available"
    assert len(result["rankings"]) == 16
    assert result["metadata"]["unique_cell_count"] == 8
    assert result["metadata"]["bundle"]["bundle_sha256"] == EXPECTED_BUNDLE_SHA256
    assert get_evidence_bundle_sha256() == EXPECTED_BUNDLE_SHA256
    assert all(row["raw_values"]["q3_coverage_pct"] > 0 for row in result["rankings"])
    assert {row["screening_level"] for row in result["rankings"]} <= {
        "S1",
        "S2",
        "S3",
        "S4",
    }
    expected_colors = {
        "S1": "#16a34a",
        "S2": "#f59e0b",
        "S3": "#dc2626",
        "S4": "#7f1d1d",
    }
    assert all(
        row["screening_color"] == expected_colors[row["screening_level"]]
        for row in result["rankings"]
    )
    bands = result["metadata"]["methodology"]["screening_bands"]
    assert bands == [dict(band) for band in SCREENING_BANDS]
    assert [band["level"] for band in bands] == ["S1", "S2", "S3", "S4"]
    assert bands[-1]["max_inclusive"] is True
    assert all(band["min_inclusive"] is True for band in bands)


def test_zero_q3_coverage_and_default_sha_mismatch_fail_closed(tmp_path, monkeypatch):
    names = _canonical_community_names()
    normal = build_exploratory_rankings([names[0]])
    cell_id = normal["rankings"][0]["cell_id"]
    collection = json.loads(DEFAULT_GEOJSON_PATH.read_text(encoding="utf-8"))
    changed = deepcopy(collection)
    cell = next(feature for feature in changed["features"] if feature.get("id") == cell_id)
    cell["properties"]["q3_coverage_pct"] = 0
    changed_path = tmp_path / "zero-coverage.geojson"
    changed_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")

    zero_coverage = build_exploratory_rankings([names[0]], geojson_path=changed_path)
    assert zero_coverage["status"] == "unavailable"
    assert zero_coverage["metadata"]["reason_code"] == "global_common_field_failure"
    assert zero_coverage["metadata"]["excluded_communities"][0]["invalid_fields"] == [
        "q3_coverage_pct"
    ]

    import services.community_vulnerability_evidence as evidence_module

    original_loader = evidence_module._load_collection

    def mismatched_loader(path):
        loaded, _sha256 = original_loader(path)
        return loaded, "0" * 64

    monkeypatch.setattr(evidence_module, "_load_collection", mismatched_loader)
    mismatch = build_exploratory_rankings([names[0]])
    assert mismatch["status"] == "unavailable"
    assert mismatch["metadata"]["reason_code"] == "bundle_fingerprint_mismatch"


def test_empty_community_table_still_returns_sixteen_static_rows(
    app,
    db_session,
    monkeypatch,
):
    """公开筛查必须独立于 Community、天气、病历和 DLNM。"""
    from core.db_models import Community

    db_session.query(Community).delete()
    db_session.commit()
    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", COMMUNITY_COORDS_GCJ)
    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: (_ for _ in ()).throw(AssertionError("静态筛查不应加载 DLNM")),
    )

    service = CommunityRiskService()
    direct = service.generate_exploratory_geospatial_screening()
    through_formal_entry = service.generate_community_risk_map({"temperature": 35})

    for result in (direct, through_formal_entry):
        assert result["ranking_mode"] == "exploratory_geospatial_screening"
        assert result["ranking_status"] == "available"
        assert result["summary"]["ranked_communities"] == 16
        assert result["summary"]["total_communities"] == 16
        assert len(result["rankings"]) == 16
        assert result["macro_weather"]["available"] is False
        assert result["macro_weather"]["temperature"] is None
        assert result["macro_weather"]["rr"] is None
        assert result["macro_weather"]["lag_temperatures_used"] == 0
        assert result["macro_weather"]["used_in_ranking"] is False
        assert result["macro_weather"]["role"] == "not_calculated_for_screening"
        assert result["ranking_metadata"]["weather_context_available"] is False
        assert result["ranking_metadata"]["weather_health_model_calculated"] is False
        assert result["impact_likelihood_matrix"]["data_available"] is False
        assert result["management_suggestions"] == []
        assert all(row["risk_score"] is None for row in result["rankings"])
        assert all(row["expected_excess_visits"] is None for row in result["rankings"])


def test_screening_does_not_fallback_to_profile_names_without_display_coordinates(
    app,
    db_session,
    monkeypatch,
):
    """候选总体只接受 GCJ 展示坐标与 WGS84 证据坐标的同名交集。"""
    service = CommunityRiskService()
    service.community_profiles = {
        "庙北吴村": {
            "name": "庙北吴村",
            "population": 100,
        }
    }
    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", {})

    assert service._exploratory_community_names() == []
    assert service.generate_exploratory_geospatial_screening() is None

    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", ["庙北吴村"])
    assert service._exploratory_community_names() == []
    assert service.generate_exploratory_geospatial_screening() is None


def test_complete_profile_keeps_formal_risk_track(app, db_session, monkeypatch):
    """完整画像仍执行原正式风险计算，避免筛查轨吞掉既有功能。"""
    service = CommunityRiskService()
    service.community_profiles = {
        "测试社区": {
            "id": 1,
            "name": "测试社区",
            "location": "测试地点",
            "population": 100,
            "elderly_ratio": 0.4,
            "chronic_disease_ratio": 0.15,
            "green_space_ratio": 0.1,
            "heat_island_index": 0.5,
            "medical_accessibility": 0.6,
            "baseline_visits": 5.0,
            "uses_proxy_values": False,
        }
    }
    monkeypatch.setattr(service, "_load_community_profiles", lambda: None)

    class StubDLNM:
        def calculate_rr(self, _temperature, lag_temperatures=None):
            return 1.8, {"lag_temperatures": lag_temperatures}

    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: StubDLNM(),
    )

    result = service.generate_community_risk_map(_trusted_weather(35))

    assert result.get("ranking_mode") != "exploratory_geospatial_screening"
    assert result["summary"]["ranked_communities"] == 1
    assert len(result["rankings"]) == 1
    assert result["rankings"][0]["risk_score"] is not None
    assert result["rankings"][0]["expected_excess_visits"] is not None


def test_cache_v5_tracks_path_and_signature_and_does_not_cache_none(app, monkeypatch):
    import services.community_risk_cache as cache_module

    cache_module.clear_local_community_risk_cache()
    monkeypatch.setattr(cache_module, "_get_redis_client", lambda: None)
    base = cache_module.build_community_risk_cache_params(
        city="都昌县",
        ranking_path="auto",
        input_signature="bundle-a",
    )
    screening = cache_module.build_community_risk_cache_params(
        city="都昌县",
        ranking_path="exploratory_only",
        input_signature="bundle-a",
    )
    changed = cache_module.build_community_risk_cache_params(
        city="都昌县",
        ranking_path="auto",
        input_signature="bundle-b",
    )

    assert base["ranking_contract"] == RANKING_METHOD_VERSION
    assert len({
        cache_module._build_cache_key(base),
        cache_module._build_cache_key(screening),
        cache_module._build_cache_key(changed),
    }) == 3

    calls = {"count": 0}

    def builder():
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return {"ok": True}

    with app.app_context():
        first, first_hit = cache_module.get_or_build_community_risk_result(base, builder)
        second, second_hit = cache_module.get_or_build_community_risk_result(base, builder)

    assert (first, first_hit) == (None, False)
    assert (second, second_hit) == ({"ok": True}, False)
    assert calls["count"] == 2
    cache_module.clear_local_community_risk_cache()


def test_service_input_signature_changes_with_display_coordinates(
    app,
    db_session,
    monkeypatch,
):
    """网页坐标或证据输入变化后必须产生新缓存指纹。"""
    coords = dict(COMMUNITY_COORDS_GCJ)
    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", coords)
    service = CommunityRiskService()
    first = service.get_ranking_input_signature()

    changed_coords = dict(coords)
    first_name = next(iter(changed_coords))
    longitude, latitude = changed_coords[first_name]
    changed_coords[first_name] = [longitude + 0.0001, latitude]
    monkeypatch.setitem(app.config, "COMMUNITY_COORDS_GCJ", changed_coords)
    second = service.get_ranking_input_signature()

    assert len(first) == 64
    assert len(second) == 64
    assert first != second


def test_api_serves_screening_without_weather_and_valid_empty_result_is_503(
    authenticated_client,
    monkeypatch,
):
    from services.community_risk_cache import clear_local_community_risk_cache

    clear_local_community_risk_cache()
    calls = {"screening": 0, "formal": 0}

    class ScreeningService:
        def get_ranking_input_signature(self):
            return "screening-input"

        def generate_exploratory_geospatial_screening(self, **_kwargs):
            calls["screening"] += 1
            return _minimal_result("exploratory_geospatial_screening")

        def generate_community_risk_map(self, *_args, **_kwargs):
            calls["formal"] += 1
            raise AssertionError("无有效天气时不应调用正式风险轨")

    monkeypatch.setattr(
        "services.api_service.get_weather_with_cache",
        lambda _city: ({"temperature": 35, "is_mock": True, "data_source": "Demo"}, False),
    )
    monkeypatch.setattr(
        "services.community_risk_service.get_community_service",
        lambda: ScreeningService(),
    )
    headers = {"X-CSRF-Token": "test-csrf-token"}
    request_json = {"analysis_date": "2026-08-29", "window_days": 30, "city": "都昌"}

    first = authenticated_client.post(
        "/api/community/risk-map-v2",
        json=request_json,
        headers=headers,
    )
    second = authenticated_client.post(
        "/api/community/risk-map-v2",
        json=request_json,
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["ranking_mode"] == "exploratory_geospatial_screening"
    assert first.get_json()["cache_hit"] is False
    assert second.get_json()["cache_hit"] is True
    assert calls == {"screening": 1, "formal": 0}

    class EmptyFormalService:
        def get_ranking_input_signature(self):
            return "formal-empty-input"

        def generate_community_risk_map(self, *_args, **_kwargs):
            return None

    clear_local_community_risk_cache()
    monkeypatch.setattr(
        "services.api_service.get_weather_with_cache",
        lambda _city: (_trusted_weather(), True),
    )
    monkeypatch.setattr(
        "services.community_risk_service.get_community_service",
        lambda: EmptyFormalService(),
    )
    unavailable = authenticated_client.post(
        "/api/community/risk-map-v2",
        json=request_json,
        headers=headers,
    )

    assert unavailable.status_code == 503
    assert unavailable.get_json()["error"] == "community_risk_unavailable"
    clear_local_community_risk_cache()


def test_real_api_with_valid_weather_and_empty_community_table_returns_sixteen_rows(
    authenticated_client,
    monkeypatch,
):
    """有效天气也不能让空 Community 表提前截断静态筛查。"""
    import services.community_risk_service as risk_module
    from services.community_risk_cache import clear_local_community_risk_cache

    clear_local_community_risk_cache()
    monkeypatch.setattr(
        "services.api_service.get_weather_with_cache",
        lambda _city: (_trusted_weather(31), True),
    )
    monkeypatch.setattr(risk_module, "_community_service", None)

    response = authenticated_client.post(
        "/api/community/risk-map-v2",
        json={"analysis_date": "2026-08-29", "window_days": 30, "city": "都昌"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ranking_mode"] == "exploratory_geospatial_screening"
    assert payload["ranking_status"] == "available"
    assert payload["summary"]["ranked_communities"] == 16
    assert len(payload["rankings"]) == 16
    assert all(row["risk_index"] is None for row in payload["rankings"])
    clear_local_community_risk_cache()


def test_precompute_uses_screening_path_when_weather_is_unavailable(app, monkeypatch):
    from services.community_risk_cache import clear_local_community_risk_cache
    from services.pipelines.precompute_community_risk import precompute_community_risk

    clear_local_community_risk_cache()
    calls = {"screening": 0, "formal": 0}

    class ScreeningService:
        def get_ranking_input_signature(self):
            return "precompute-screening-input"

        def generate_exploratory_geospatial_screening(self, **_kwargs):
            calls["screening"] += 1
            return _minimal_result("exploratory_geospatial_screening")

        def generate_community_risk_map(self, *_args, **_kwargs):
            calls["formal"] += 1
            return _minimal_result()

    monkeypatch.setattr(
        "services.pipelines.precompute_community_risk.get_weather_with_cache",
        lambda _location, cache_only=True: (
            {"temperature": 35, "is_mock": True, "data_source": "Demo"},
            False,
        ),
    )
    monkeypatch.setattr(
        "services.pipelines.precompute_community_risk.get_community_service",
        lambda: ScreeningService(),
    )

    summary = precompute_community_risk(
        app=app,
        locations=["都昌"],
        window_days_list=[30],
        disease_filters=[""],
        analysis_date=datetime(2026, 8, 29).date(),
    )

    assert summary["screening_only"] == 1
    assert summary["weather_skipped"] == 1
    assert summary["computed"] == 1
    assert summary["combinations"] == 1
    assert calls == {"screening": 1, "formal": 0}
    clear_local_community_risk_cache()
