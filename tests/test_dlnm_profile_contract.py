# -*- coding: utf-8 -*-
"""DLNM 固化画像的研究边界回归测试。"""

import pytest


def _profile_service():
    from services.dlnm_risk_service import DLNMRiskService, LITERATURE_PRIORS

    service = object.__new__(DLNMRiskService)
    service.model_trained = True
    service.model_source = 'calibrated_profile'
    service.profile_loaded = True
    service.profile_name = 'final_single_model_ar1'
    service.profile_path = None
    service.model_profile_metrics = {}
    service.mmt = 23.8
    service.tmin_p90 = 25.0
    service.percentiles = {
        'p5': 2.0,
        'p10': 5.0,
        'p90': 32.0,
        'p95': 35.0,
    }
    service.max_lag = 7
    service.max_lag_cold = 14
    service.rr_cap_single = 2.6
    service.rr_cap_cumulative = 3.5
    service.sample_counts = {}
    service.disease_specific_rr = {}
    service.seasonal_baseline = {}
    service.literature_weight = 0.5
    service.literature_priors = LITERATURE_PRIORS
    service._get_base_rr = lambda _temperature: 1.25
    return service


def test_profile_curve_is_not_weighted_again_by_online_lag_history():
    service = _profile_service()

    def unexpected_online_weighting(*_args, **_kwargs):
        raise AssertionError('固化画像已是累积 RR，不能再次在线加权')

    service._apply_lag_effects = unexpected_online_weighting

    rr, breakdown = service.calculate_rr(5, lag_temperatures=[5] * 15)

    assert rr == pytest.approx(1.25)
    assert breakdown['lag_history_applied'] is False
    assert breakdown['lag_history_status'] == 'profile_cumulative_rr_no_online_reweight'


def test_research_rr_priors_are_metadata_only_and_model_is_exploratory():
    service = _profile_service()

    summary = service.get_model_summary()

    assert summary['evidence_status'] == 'exploratory'
    assert summary['mmt_boundary_flag'] == 1
    assert summary['probability_calibrated'] is False
    assert summary['threshold_semantics'] == 'action_communication_interface'
    assert summary['research_priors']['cold_p5_rr'] == {
        'value': 1.45,
        'applied': False,
        'status': 'inactive_research_metadata',
    }
    assert summary['research_priors']['hot_night_rr'] == {
        'value': 1.34,
        'applied': False,
        'status': 'inactive_research_metadata',
    }


def test_hot_night_signal_does_not_apply_an_unvalidated_rr_multiplier():
    service = _profile_service()

    events = service.identify_extreme_weather_events(26, is_night_temp=True)
    hot_night = next(item for item in events if item['type'] == '热夜')

    assert 'rr_multiplier' not in hot_night
    assert hot_night['model_effect_applied'] is False
    assert hot_night['effect_status'] == 'action_signal_only'
