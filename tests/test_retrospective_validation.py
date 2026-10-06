"""合成序列验证回顾性实验边界；真实报告只核对聚合结构与页面。"""
import json

import numpy as np
import pandas as pd
import pytest

from services.model_validation_service import (
    EXPLORATORY_REPORT, _snapshot_input, _retrospective_rows, get_exploratory_scorecard,
    retrospective_exploratory_report, rolling_origin_report,
)


def snapshot(n=110):
    dates = pd.date_range("2024-01-01", periods=n)
    values = (np.arange(n) % 7 + (np.arange(n) // 25) % 2).astype(float)
    return pd.DataFrame({"date": dates, "cases_60plus": values, "cases_all": values + 2,
                         "coverage_confirmed": False, "tmean": np.linspace(10, 35, n)})


def report(frame=None):
    return retrospective_exploratory_report(snapshot() if frame is None else frame,
        min_train=28, calibration_days=20, provenance={"daily_sha256": "a" * 64})


def test_exploration_scores_unverified_snapshot_without_changing_strict_gate():
    frame = snapshot()
    assert rolling_origin_report(frame)["status"] == "unavailable"
    result = report(frame)
    assert result["evaluation_type"] == "retrospective_exploratory"
    assert result["n"] == 110 - 28 - 28 - 20
    assert result["metrics"]["n"] == result["baseline"]["n"] == result["n"]
    assert result["production_model_validated"] is False
    assert result["prospective_n"] == 0
    assert result["weather_use"] == "none"
    assert result["audit"]["first_training_end"] < result["test_period"]["start"]


def test_every_origin_fit_excludes_target_and_future(monkeypatch):
    import services.model_validation_service as module
    calls = []
    def observe(train_x, train_y, target_x):
        assert train_x.index.max() < target_x.index[0]
        assert train_y.index.equals(train_x.index)
        calls.append((len(train_x), target_x.index[0]))
        return float(train_y.mean())
    monkeypatch.setattr(module, "_fit_snapshot_candidate", observe)
    result = report()
    assert len(calls) == result["audit"]["refitted_origins"]
    assert calls[0][0] == 28
    assert calls[-1][0] == 81


def test_target_and_future_outcomes_do_not_change_existing_predictions_or_intervals():
    frame = snapshot(100)
    rows, _ = _retrospective_rows(_snapshot_input(frame, "cases_60plus"), "cases_60plus", 28, 20)
    target = pd.Timestamp(rows[4]["date"])
    altered = frame.copy()
    altered.loc[altered.date >= target, "cases_60plus"] += 20
    altered.loc[altered.date >= target, "cases_all"] += 20
    changed, _ = _retrospective_rows(_snapshot_input(altered, "cases_60plus"), "cases_60plus", 28, 20)
    for old, new in zip(rows[:5], changed[:5]):
        for key in ("prediction", "baseline", "prediction_lower_80", "prediction_upper_95", "baseline_upper_95", "calibration_n"):
            assert old[key] == pytest.approx(new[key])


def test_target_weather_is_not_a_feature():
    first = report()
    frame = snapshot()
    frame["tmean"] = float("nan")
    second = report(frame)
    assert first["metrics"] == second["metrics"]


def test_sensitivity_changes_only_target_scoring_and_counts_exclusions():
    frame = snapshot()
    frame.loc[frame.index >= 95, ["cases_all", "cases_60plus"]] = 0
    result = report(frame)
    sensitivity = result["sensitivity_exclude_no_record_targets"]
    assert sensitivity["excluded_target_days"] == 15
    assert sensitivity["n"] + sensitivity["excluded_target_days"] == result["n"]
    assert sensitivity["role"] == "secondary_not_model_selection"
    assert result["coverage_confirmed"] is False


@pytest.mark.parametrize("change", ["missing_day", "missing_count", "duplicate", "negative", "noninteger", "wrong_age"])
def test_snapshot_never_repairs_bad_input_or_relabels_age(change):
    frame = snapshot()
    if change == "missing_day": frame = frame.drop(50)
    if change == "missing_count": frame.loc[50, "cases_60plus"] = np.nan
    if change == "duplicate": frame.loc[50, "date"] = frame.loc[49, "date"]
    if change == "negative": frame.loc[50, "cases_60plus"] = -1
    if change == "noninteger": frame.loc[50, "cases_60plus"] = 0.5
    if change == "wrong_age": frame = frame.rename(columns={"cases_60plus": "elderly_cases"})
    with pytest.raises(ValueError): report(frame)


def test_zero_training_period_is_handled_without_pretending_confirmed_zeros():
    frame = snapshot()
    frame[["cases_60plus", "cases_all"]] = 0
    result = report(frame)
    assert result["metrics"]["mae"] == 0
    assert result["mae_improvement_vs_baseline"] is None
    assert result["coverage_confirmed"] is False
    assert result["sensitivity_exclude_no_record_targets"]["n"] == 0


@pytest.mark.parametrize("mutation", ["upgrade", "nan_width", "wrong_n", "missing_hash"])
def test_public_exploration_rejects_false_claims_and_corruption(tmp_path, mutation):
    value = report()
    if mutation == "upgrade": value["production_model_validated"] = True
    if mutation == "nan_width": value["metrics"]["mean_interval_width_95"] = float("nan")
    if mutation == "wrong_n": value["baseline"]["n"] += 1
    if mutation == "missing_hash": value["provenance"] = {}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(value))
    assert get_exploratory_scorecard(path) is None


def test_report_writer_keeps_modes_and_output_paths_separate(tmp_path):
    from scripts.backtest_forecast import backtest
    frame = snapshot(180)
    data = tmp_path / "aggregate.csv"
    frame.to_csv(data, index=False)
    strict_path, strict = backtest(data, output_path=tmp_path / "strict.json")
    path, exploration = backtest(data, output_path=tmp_path / "exploration.json", mode="retrospective_exploratory")
    assert strict["status"] == "unavailable"
    assert exploration["n"] > 0
    assert get_exploratory_scorecard(path)["n"] == exploration["n"]
    assert json.loads(strict_path.read_text())["status"] == "unavailable"


def test_committed_real_report_is_aggregate_and_both_results_visible(client):
    result = get_exploratory_scorecard(EXPLORATORY_REPORT)
    assert result is not None
    assert result["input_summary"]["n"] == 415
    assert result["n"] == 275
    assert result["exclusions"]["total_not_scored"] == 140
    assert result["provenance"]["ledger_eligible_primary_n"] == 0
    serialized = json.dumps(result)
    for forbidden in ("patient_name", "insurance_id", "chief_complaint", '"actual"', '"rows"'):
        assert forbidden not in serialized
    response = client.get("/transparency")
    assert response.status_code == 200
    assert "这是记录计数回顾性比较，尚未验证高温健康模型" in response.text
    assert "敏感性结果" in response.text
    assert "事前归档与前瞻验证" in response.text
    assert "生产模型尚无可核验的事前归档验证" in response.text
    assert str(result["metrics"]["mae"]) in response.text
    assert str(result["sensitivity_exclude_no_record_targets"]["metrics"]["mae"]) in response.text


@pytest.mark.parametrize("section,key", [("design", "min_train_rows"), ("input_summary", "n"), ("exclusions", "total_not_scored"), ("metrics", "coverage_80")])
def test_missing_template_contract_fails_closed(tmp_path, section, key):
    value = report()
    value[section].pop(key)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(value))
    assert get_exploratory_scorecard(path) is None


def test_candidate_and_interval_metrics_are_reproducible():
    first, second = report(), report()
    for key in ("metrics", "baseline", "test_period", "exclusions", "sensitivity_exclude_no_record_targets"):
        assert first[key] == second[key]


def test_mismatched_coverage_ledger_cannot_be_used_as_lineage(tmp_path):
    from scripts.backtest_forecast import backtest
    frame = snapshot(180)
    data, ledger = tmp_path / "data.csv", tmp_path / "ledger.csv"
    frame.to_csv(data, index=False)
    pd.DataFrame({"date": frame.date, "outcome": "cases_60plus", "observed_count": frame.cases_60plus + 1, "eligible_primary": False}).to_csv(ledger, index=False)
    with pytest.raises(ValueError, match="不匹配"):
        backtest(data, mode="retrospective_exploratory", coverage_path=ledger, output_path=tmp_path / "output.json")
    assert not (tmp_path / "output.json").exists()
