"""锁定脆弱性单次计权、置信度独立与历史 GIS 边界。"""
import json
from pathlib import Path

import pytest

from scripts.community_risk_sensitivity import analyze, comparison, synthetic_payload
from services.community_risk_service import CommunityRiskService


def test_hazard_unchanged_when_only_vulnerability_changes():
    service = CommunityRiskService()
    common = dict(baseline_period_days=1, baseline_population_scope="all_residents", population=100, baseline_visits=5, elderly_ratio=0.2,
                  chronic_disease_ratio=0.1, green_space_ratio=0.2,
                  heat_island_index=0.3, medical_accessibility=0.7)
    service.community_profiles = {"a": common, "b": {**common, "elderly_ratio": 0.8}}
    a = service.calculate_community_risk_score("a", 2)
    b = service.calculate_community_risk_score("b", 2)
    assert a["vi_details"]["vulnerability_index"] != b["vi_details"]["vulnerability_index"]
    assert a["hazard_formula"] == b["hazard_formula"]
    assert a["expected_excess_visits"] == b["expected_excess_visits"] == 5


@pytest.mark.parametrize("historical,certainty,uncertainty", [(False, "unavailable", None), (True, "low", 90), (True, "high", 75)])
def test_high_risk_with_low_confidence_requires_verification(historical, certainty, uncertainty):
    result = CommunityRiskService._confidence_assessment(
        {"historical_component_available": historical, "certainty": certainty,
         "uncertainty_index": uncertainty}, 75)
    assert result["confidence"] == "low"
    assert result["verification_priority"] is True
    assert result["review_status"] == "优先核实"


def test_low_confidence_does_not_change_risk_ranking(monkeypatch):
    service = CommunityRiskService()
    common = dict(baseline_period_days=1, baseline_population_scope="all_residents", population=100, baseline_visits=20, elderly_ratio=0.4,
                  chronic_disease_ratio=0.3, green_space_ratio=0.1,
                  heat_island_index=0.8, medical_accessibility=0.2)
    service.community_profiles = {"a": common, "b": {**common, "elderly_ratio": 0.5}}
    monkeypatch.setattr("services.dlnm_risk_service.get_dlnm_service", lambda: type("Model", (), {"calculate_rr": lambda *a, **k: (3, {})})())
    monkeypatch.setattr(service, "_collect_medical_counts", lambda *a, **k: {
        "counts_by_community": {"a": 1, "b": 1}, "window_days": 30,
        "matched_records": 2, "total_records": 2, "unmatched_records": 0})
    result = service.generate_community_risk_map({"temperature": 35})
    for row in result["rankings"]:
        assert row["uncertainty_index"] >= 70
        assert row["uncertainty_penalty"] == 1
        contributions = row["risk_contributions"]
        assert contributions["before_penalty"] == contributions["after_penalty"]
        assert row["confidence"] == "low"
        if row["risk_index"] >= 60:
            assert row["verification_priority"]
    assert result["summary"]["verification_priority_count"] > 0


def test_sensitivity_reports_both_weight_directions_and_synthetic_boundary():
    report = analyze(synthetic_payload())
    assert report["source_kind"] == "synthetic"
    assert report["sample_size"] == 40
    assert len(report["weight_perturbations"]) == 6
    assert {row["multiplier"] for row in report["weight_perturbations"]} == {0.8, 1.2}
    assert report["old_vs_new"]["kendall_tau_b"] < 1
    assert "不是实测验证" in report["meaning"]
    json.dumps(report, allow_nan=False)
    assert comparison([1, 1], [2, 2])["kendall_tau_b"] is None
    with pytest.raises(ValueError):
        analyze({"rankings": []})


def test_static_gis_explicitly_disallows_realtime_weather(authenticated_client):
    html = authenticated_client.get("/heat-exposure-gis?ui=legacy").get_data(as_text=True)
    assert "结构脆弱性 GIS" in html
    assert "2020 年底数" in html
    assert "2020–2024 年历史夏季观测，不叠加实时天气" in html
    assert "实时风险只在县级展示" in html
    root = Path(__file__).resolve().parents[1]
    metadata = json.loads((root / "data/gis/duchang_heat_exposure_cells.geojson").read_text())["metadata"]
    assert metadata["study_period"]["end"].startswith("2024")


@pytest.mark.parametrize("rr", [None, "bad", float("nan"), float("inf"), 0, -1])
def test_missing_rr_never_becomes_no_excess_risk(rr):
    service = CommunityRiskService()
    service.community_profiles = {"a": dict(baseline_period_days=1, baseline_population_scope="all_residents", population=100, baseline_visits=5, elderly_ratio=0.2,
        chronic_disease_ratio=0.1, green_space_ratio=0.2, heat_island_index=0.3, medical_accessibility=0.7)}
    row = service.calculate_community_risk_score("a", rr)
    assert row["ranking_eligible"] is False
    assert row["normalized_score"] is None
    assert row["data_status"] == "weather_rr_unknown"


@pytest.mark.parametrize("weather", [{}, {"temperature": None}, {"temperature": float("nan")}, {"temperature": 35, "is_mock": True}])
def test_missing_weather_propagates_unknown(weather):
    service = CommunityRiskService()
    service.community_profiles = {"a": dict(baseline_period_days=1, baseline_population_scope="all_residents", population=100, baseline_visits=5, elderly_ratio=0.2,
        chronic_disease_ratio=0.1, green_space_ratio=0.2, heat_island_index=0.3, medical_accessibility=0.7)}
    result = service.generate_community_risk_map(weather)
    assert result["data_available"] is False
    assert result["rankings"] == []
    assert result["input_states"]["temperature"]["status"] == "unknown"
    assert result["macro_weather"]["rr"] is None
