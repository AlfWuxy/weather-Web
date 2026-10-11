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
    assert summary['mmt_boundary_flag'] is None
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


@pytest.mark.parametrize('raw, expected', [
    (None, None), (0, 0), (1, 1), (0.0, 0), (1.0, 1),
    (True, None), (False, None), ('1', None), (2, None),
    (-1, None), (0.5, None), (float('nan'), None), (float('inf'), None),
])
def test_summary_preserves_mmt_diagnostic_and_keeps_invalid_values_unknown(raw, expected):
    service = _profile_service()
    service.model_profile_metrics = {'mmt_boundary_flag': raw}
    summary = service.get_model_summary()
    assert summary['mmt_boundary_flag'] == expected
    assert summary['evidence_status'] == 'exploratory'
    assert summary['probability_calibrated'] is False


@pytest.mark.parametrize('flag, label', [
    (None, '诊断资料待补充'),
    (0, '参考温度位于搜索范围内'),
    (1, '参考温度位于搜索边界'),
])
def test_transparency_reads_configured_diagnostic_without_loading_health_data(client, tmp_path, monkeypatch, flag, label):
    import json
    from services.dlnm_risk_service import DLNMRiskService, get_configured_profile_metadata

    profile = tmp_path / 'private-profile.json'
    profile.write_text(json.dumps({
        'version': 'test-version', 'mmt': 24.2,
        'curve': [{'temp': 24.2, 'rr': 1}],
        'metrics': {} if flag is None else {'mmt_boundary_flag': flag},
        'source': '/private/health/records', 'sample_counts': {'private_cohort': 9},
    }))
    monkeypatch.setenv('DLNM_PROFILE_PATH', str(profile))
    monkeypatch.setenv('DLNM_USE_PROFILE', 'true')
    monkeypatch.setattr(DLNMRiskService, '__init__', lambda *args, **kwargs: pytest.fail('方法页应只读取公开配置元数据'))
    metadata = get_configured_profile_metadata()
    assert metadata == {'status': 'available', 'version': 'test-version', 'mmt': 24.2, 'mmt_boundary_flag': flag}
    response = client.get('/transparency')
    assert response.status_code == 200
    assert label in response.text
    assert 'test-version' in response.text
    assert '24.2°C' in response.text
    assert '/private/health/records' not in response.text
    assert 'private_cohort' not in response.text
    assert 'MMT 边界标记为 1' not in response.text


def test_public_profile_metadata_handles_disabled_and_missing_configuration(monkeypatch, tmp_path):
    from services.dlnm_risk_service import get_configured_profile_metadata
    monkeypatch.setenv('DLNM_PROFILE_PATH', str(tmp_path / 'missing.json'))
    monkeypatch.setenv('DLNM_USE_PROFILE', 'false')
    assert get_configured_profile_metadata() == {'status': 'disabled'}
    monkeypatch.setenv('DLNM_USE_PROFILE', 'true')
    assert get_configured_profile_metadata() == {'status': 'unavailable'}


def test_public_profile_metadata_matches_explicit_empty_loader_path(monkeypatch):
    from services.dlnm_risk_service import get_configured_profile_metadata
    monkeypatch.setenv('DLNM_USE_PROFILE', 'true')
    monkeypatch.setenv('DLNM_PROFILE_PATH', '')
    # 加载器遇到显式空路径会读取目录失败，方法页应显示同一资料状态。
    assert get_configured_profile_metadata() == {'status': 'unavailable'}
