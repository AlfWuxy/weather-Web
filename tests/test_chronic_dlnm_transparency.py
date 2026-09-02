# -*- coding: utf-8 -*-
"""慢病与 DLNM 双层修正透明度回归测试。"""

import pytest


def test_dlnm_breakdown_exposes_raw_and_internal_modifiers():
    from services.dlnm_risk_service import DLNMRiskService

    service = object.__new__(DLNMRiskService)
    service.model_trained = True
    service.mmt = 20.0
    service.disease_specific_rr = {
        'cardiovascular': {
            'heat_sensitivity': 1.2,
            'cold_sensitivity': 1.1,
            'age_modifier': lambda _age: 1.1,
        }
    }
    service.rr_cap_cumulative = 3.5
    service.literature_weight = 0.3
    service.literature_priors = {'age_modifiers': {}}
    service._get_base_rr = lambda _temperature: 1.25

    final_rr, breakdown = service.calculate_rr(32, disease_type='cardiovascular', age=70)

    assert final_rr == pytest.approx(1.25 * 1.2 * 1.1)
    assert breakdown['raw_dlnm_rr'] == pytest.approx(1.25)
    assert breakdown['dlnm_disease_modifier'] == pytest.approx(1.2)
    assert breakdown['dlnm_age_modifier'] == pytest.approx(1.1)
    assert breakdown['dlnm_adjusted_rr'] == pytest.approx(final_rr)
    assert breakdown['uncapped_final_rr'] == pytest.approx(final_rr)
    assert breakdown['rr_cap'] == pytest.approx(3.5)
    assert breakdown['rr_cap_applied'] is False


def test_dlnm_calculate_rr_does_not_invent_20_when_temperature_missing():
    from services.dlnm_risk_service import DLNMRiskService

    service = object.__new__(DLNMRiskService)
    service.model_trained = False

    with pytest.raises(ValueError, match='气温'):
        service.calculate_rr(None)
    with pytest.raises(ValueError, match='气温'):
        service.calculate_rr('bad')


def test_dlnm_untrained_does_not_invent_rr_from_20c():
    from services.dlnm_risk_service import DLNMRiskService

    service = object.__new__(DLNMRiskService)
    service.model_trained = False

    final_rr, breakdown = service.calculate_rr(30)

    assert final_rr is None
    assert breakdown['calculation_branch'] == 'untrained_unavailable'
    assert breakdown.get('mmt') is None
    assert breakdown.get('final_rr') is None


def test_dlnm_trained_without_mmt_does_not_invent_20_or_23():
    from services.dlnm_risk_service import DLNMRiskService

    service = object.__new__(DLNMRiskService)
    service.model_trained = True
    service.mmt = None
    service.disease_specific_rr = {}
    service.rr_cap_cumulative = 3.5
    service.literature_weight = 0.3
    service.literature_priors = {'age_modifiers': {}}
    service._get_base_rr = lambda _temperature: 1.25

    final_rr, breakdown = service.calculate_rr(32)

    assert final_rr is None
    assert breakdown.get('mmt') is None
    assert breakdown['calculation_branch'] == 'mmt_unavailable'


def test_dlnm_breakdown_marks_cap_without_changing_result():
    from services.dlnm_risk_service import DLNMRiskService

    service = object.__new__(DLNMRiskService)
    service.model_trained = True
    service.mmt = 20.0
    service.disease_specific_rr = {
        'cardiovascular': {
            'heat_sensitivity': 1.2,
            'cold_sensitivity': 1.1,
            'age_modifier': lambda _age: 1.1,
        }
    }
    service.rr_cap_cumulative = 3.5
    service.literature_weight = 0.3
    service.literature_priors = {'age_modifiers': {}}
    service._get_base_rr = lambda _temperature: 3.0

    final_rr, breakdown = service.calculate_rr(32, disease_type='cardiovascular', age=70)

    assert breakdown['uncapped_final_rr'] == pytest.approx(3.0 * 1.2 * 1.1)
    assert final_rr == pytest.approx(3.5)
    assert breakdown['dlnm_adjusted_rr'] == pytest.approx(3.5)
    assert breakdown['rr_cap_applied'] is True


def test_chronic_service_exposes_both_modifier_layers(monkeypatch):
    from services.chronic_risk_service import ChronicRiskService

    class FakeDLNMService:
        def calculate_rr(self, temperature, lag_temperatures=None, disease_type=None, age=None):
            del temperature, lag_temperatures, disease_type, age
            raw_rr = 1.2
            disease_modifier = 1.1
            age_modifier = 1.3
            adjusted_rr = raw_rr * disease_modifier * age_modifier
            return adjusted_rr, {
                'raw_dlnm_rr': raw_rr,
                'dlnm_disease_modifier': disease_modifier,
                'dlnm_age_modifier': age_modifier,
                'uncapped_final_rr': adjusted_rr,
                'dlnm_adjusted_rr': adjusted_rr,
                'rr_cap': 3.5,
                'rr_cap_applied': False,
                'calculation_branch': 'trained_model',
            }

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: FakeDLNMService(),
    )

    service = ChronicRiskService()
    result = service.predict_individual_risk(
        {'age': 45, 'gender': '男', 'chronic_diseases': ['高血压']},
        {'temperature': 32, 'humidity': 60, 'aqi': 50},
        target_diseases=['cardiovascular'],
    )
    risk = result['disease_risks']['cardiovascular']

    assert risk['raw_dlnm_rr'] == pytest.approx(1.2)
    assert risk['dlnm_disease_modifier'] == pytest.approx(1.1)
    assert risk['dlnm_age_modifier'] == pytest.approx(1.3)
    assert risk['dlnm_adjusted_rr'] == pytest.approx(1.716)
    assert risk['chronic_age_amplifier'] == pytest.approx(1.1)
    assert risk['comorbidity_amplifier'] == pytest.approx(1.4)
    assert risk['personal_rr'] == pytest.approx(1.716 * 1.1 * 1.4, abs=0.001)


def test_chronic_service_fails_closed_when_dlnm_rr_unavailable(monkeypatch):
    from services.chronic_risk_service import ChronicRiskService

    class UnavailableDLNMService:
        def calculate_rr(self, temperature, lag_temperatures=None, disease_type=None, age=None):
            del temperature, lag_temperatures, disease_type, age
            return None, {
                'error': '模型未训练，相对风险暂不计算',
                'calculation_branch': 'untrained_unavailable',
                'final_rr': None,
            }

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: UnavailableDLNMService(),
    )

    service = ChronicRiskService()
    result = service.predict_individual_risk(
        {'age': 45, 'gender': '男', 'chronic_diseases': ['高血压']},
        {'temperature': 32, 'humidity': 60, 'aqi': 50},
        target_diseases=['cardiovascular'],
    )

    assert result['disease_risks'] == {}
    overall = result['overall_risk']
    assert overall.get('rr') is None
    assert overall.get('score') is None
    assert overall.get('level') is None
    assert overall.get('rr') != 1.0
