# -*- coding: utf-8 -*-
"""个人风险输出的年龄、缺失 AQI 与概率语义回归测试。"""

import pytest


class RecordingDLNM:
    def __init__(self):
        self.ages = []

    def calculate_rr(self, temperature, lag_temperatures=None, disease_type=None, age=None):
        del temperature, lag_temperatures, disease_type
        self.ages.append(age)
        return 1.2, {
            'raw_dlnm_rr': 1.2,
            'dlnm_disease_modifier': 1.0,
            'dlnm_age_modifier': 1.0,
            'uncapped_final_rr': 1.2,
            'dlnm_adjusted_rr': 1.2,
            'rr_cap': 3.5,
            'rr_cap_applied': False,
            'calculation_branch': 'trained_model',
        }


def test_chronic_risk_uses_exactly_one_personal_age_layer(monkeypatch):
    from services.chronic_risk_service import ChronicRiskService

    dlnm = RecordingDLNM()
    monkeypatch.setattr('services.dlnm_risk_service.get_dlnm_service', lambda: dlnm)

    result = ChronicRiskService().predict_individual_risk(
        {'age': 75, 'gender': '女', 'chronic_diseases': []},
        {'temperature': 32, 'humidity': 60, 'aqi': 50},
        target_diseases=['cardiovascular'],
    )
    risk = result['disease_risks']['cardiovascular']

    assert dlnm.ages == [None]
    assert risk['dlnm_age_modifier'] == pytest.approx(1.0)
    assert risk['chronic_age_amplifier'] == pytest.approx(1.8)
    assert risk['personal_rr'] == pytest.approx(1.2 * 1.8)
    assert risk['age_modifier_policy'] == 'chronic_layer_only'
    assert result['weather']['aqi_available'] is True


def test_missing_aqi_is_preserved_and_never_triggers_air_quality_rules(monkeypatch):
    from services.chronic_risk_service import ChronicRiskService

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: RecordingDLNM(),
    )

    result = ChronicRiskService().predict_individual_risk(
        {'age': 70, 'gender': '男', 'chronic_diseases': ['高血压']},
        {'temperature': 24, 'humidity': 60, 'aqi': None},
        target_diseases=['cardiovascular'],
    )

    assert result['weather']['aqi'] is None
    assert result['weather']['aqi_available'] is False
    assert all(not item['rule_id'].startswith('aqi_') for item in result['triggered_rules'])
    assert isinstance(result['explain']['escalation'], list)


@pytest.mark.parametrize('tmin', [None, 'invalid', float('nan')])
def test_invalid_night_temperature_does_not_trigger_or_crash(monkeypatch, tmin):
    from services.chronic_risk_service import ChronicRiskService

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: RecordingDLNM(),
    )

    result = ChronicRiskService().predict_individual_risk(
        {'age': 70, 'gender': '女', 'chronic_diseases': []},
        {'temperature': 24, 'humidity': 60, 'aqi': 50, 'tmin': tmin},
        target_diseases=['general'],
    )

    assert all(item['rule_id'] != 'heat_night' for item in result['triggered_rules'])
