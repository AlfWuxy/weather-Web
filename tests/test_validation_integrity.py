"""验证边界测试只使用明确的合成日序列。"""
import json

import numpy as np
import pandas as pd
import pytest

from services.model_validation_service import get_validation_scorecard, rolling_origin_report
from services.research_prior import update_coefficient_prior


def daily():
    dates = pd.date_range("2024-01-01", periods=65)
    return pd.DataFrame({"date": dates, "cases_60plus": np.arange(65) % 7 + 1,
                         "coverage_confirmed": True,
                         "available_at": (dates + pd.Timedelta(days=1)).tz_localize("Asia/Shanghai")})


def archive():
    return pd.DataFrame([{"target_date": "2024-03-01", "issued_at": "2024-02-29T12:00:00+08:00",
                          "training_end": "2024-02-28T23:59:00+08:00", "inputs_available_at": "2024-02-29T10:00:00+08:00",
                          "prediction": 5, "outcome": "cases_60plus", "model_version": "synthetic-fixture",
                          "lower_80": 4, "upper_80": 6, "lower_95": 3, "upper_95": 7}])


def test_seasonal_baseline_is_not_production_validation():
    result = rolling_origin_report(daily())
    assert result["status"] == "baseline_only"
    assert result["metrics"] is None
    assert result["baseline"]["mae"] == 0
    assert result["baseline"]["coverage_80"] == 1
    assert result["prospective_n"] == 0


def test_unconfirmed_days_never_become_zero():
    frame = daily()
    frame["coverage_confirmed"] = False
    assert rolling_origin_report(frame)["n"] == 0


@pytest.mark.parametrize("field,value", [("training_end", "2024-03-01T00:00:00+08:00"),
                                          ("inputs_available_at", "2024-03-01T00:00:00+08:00"),
                                          ("issued_at", "2024-03-01T00:00:00+08:00"),
                                          ("outcome", "deaths")])
def test_future_information_and_outcome_mismatch_rejected(field, value):
    predictions = archive()
    predictions[field] = value
    with pytest.raises(ValueError):
        rolling_origin_report(daily(), predictions=predictions)


def test_archived_prediction_scores_same_period_and_does_not_train_on_target():
    result = rolling_origin_report(daily(), predictions=archive())
    assert result["metrics"]["mae"] == 0
    assert result["n"] == result["baseline"]["n"] == 1
    assert result["metrics"]["coverage_95"] == 1
    assert result["evaluation_type"] == "archived_out_of_time"


def test_delayed_observations_are_not_available_to_baseline():
    frame = daily()
    frame.loc[frame.date == pd.Timestamp("2024-02-23"), "available_at"] = pd.Timestamp("2024-03-02", tz="Asia/Shanghai")
    assert rolling_origin_report(frame, predictions=archive())["n"] == 0


def test_report_rejects_legacy_training_fit(tmp_path):
    path = tmp_path / "score.json"
    path.write_text(json.dumps({"n_days": 408, "mae": 0.2}))
    assert get_validation_scorecard(path)["status"] == "unavailable"


def test_public_scorecard_without_report_is_honest(client):
    result = client.get("/transparency")
    assert result.status_code == 200
    assert "公开验证成绩单" in result.get_data(as_text=True)
    assert "生产模型尚无可核验的事前归档验证" in result.get_data(as_text=True)


def prior_inputs():
    contract = {"outcome": "visits_60plus", "age_group": "60+", "exposure_unit": "C",
                "basis_id": "fixture-only", "lag_days": 7, "reference_temperature": 25}
    return ({**contract, "coefficients": [0.1], "covariance": [[1]], "source_doi": "fixture-only", "reviewed": True},
            {**contract, "coefficients": [0.5], "covariance": [[1]], "coverage_confirmed": True, "training_end": "2024-12-31"})


def test_prior_update_stays_research_and_more_precise_local_data_has_more_weight():
    prior, local = prior_inputs()
    result = update_coefficient_prior(prior, local)
    assert result["coefficients"][0] == pytest.approx(0.3)
    assert result["production_ready"] is False
    local["covariance"] = [[0.1]]
    assert update_coefficient_prior(prior, local)["coefficients"][0] > 0.3


def test_prior_cannot_transfer_mortality_to_visits():
    prior, local = prior_inputs()
    prior["outcome"] = "deaths"
    with pytest.raises(ValueError):
        update_coefficient_prior(prior, local)


def test_baseline_rejects_delayed_history():
    frame = daily()
    frame["available_at"] = "2025-01-01T00:00:00+08:00"
    assert rolling_origin_report(frame)["n"] == 0


@pytest.mark.parametrize("payload", [[], {"schema_version": 2, "status": "evaluated"}])
def test_malformed_report_fails_closed(tmp_path, payload):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload))
    assert get_validation_scorecard(path)["status"] == "unavailable"


def test_impossible_metrics_fail_closed(tmp_path):
    report = rolling_origin_report(daily(), predictions=archive())
    report["metrics"]["coverage_95"] = 5
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    assert get_validation_scorecard(path)["status"] == "unavailable"


def test_timestamp_target_is_rejected_instead_of_silent_drop():
    forecasts = archive()
    forecasts["target_date"] = "2024-03-01T00:00:00+08:00"
    with pytest.raises(ValueError, match="target_date"):
        rolling_origin_report(daily(), predictions=forecasts)


def test_extreme_covariance_rejected():
    prior, local = prior_inputs()
    prior["covariance"] = [[1e-310]]
    with pytest.raises(ValueError):
        update_coefficient_prior(prior, local)


def test_utc_issue_time_after_local_target_start_is_rejected():
    forecasts = archive()
    forecasts['issued_at'] = '2024-02-29T20:00:00+00:00'
    with pytest.raises(ValueError):
        rolling_origin_report(daily(), predictions=forecasts)
