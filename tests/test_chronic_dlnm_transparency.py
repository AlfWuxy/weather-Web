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


def test_dlnm_untrained_branch_keeps_uniform_breakdown():
    from services.dlnm_risk_service import DLNMRiskService

    service = object.__new__(DLNMRiskService)
    service.model_trained = False

    final_rr, breakdown = service.calculate_rr(30)

    assert final_rr == pytest.approx(1.15)
    assert breakdown['calculation_branch'] == 'untrained_fallback'
    assert breakdown['raw_dlnm_rr'] == pytest.approx(final_rr)
    assert breakdown['dlnm_disease_modifier'] == pytest.approx(1.0)
    assert breakdown['dlnm_age_modifier'] == pytest.approx(1.0)
    assert breakdown['dlnm_adjusted_rr'] == pytest.approx(final_rr)


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


def test_chronic_service_uses_chronic_age_layer_only(monkeypatch):
    from services.chronic_risk_service import ChronicRiskService

    class FakeDLNMService:
        ages = []

        def calculate_rr(self, temperature, lag_temperatures=None, disease_type=None, age=None):
            del temperature, lag_temperatures, disease_type
            self.ages.append(age)
            raw_rr = 1.2
            disease_modifier = 1.1
            age_modifier = 1.0
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

    fake_dlnm = FakeDLNMService()
    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: fake_dlnm,
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
    assert fake_dlnm.ages == [None]
    assert risk['dlnm_age_modifier'] == pytest.approx(1.0)
    assert risk['dlnm_adjusted_rr'] == pytest.approx(1.32)
    assert risk['chronic_age_amplifier'] == pytest.approx(1.1)
    assert risk['comorbidity_amplifier'] == pytest.approx(1.4)
    assert risk['personal_rr'] == pytest.approx(1.32 * 1.1 * 1.4, abs=0.001)
    assert risk['age_modifier_policy'] == 'chronic_layer_only'
