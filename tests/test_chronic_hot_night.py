# -*- coding: utf-8 -*-
"""慢病热夜规则：夜温读 temperature_min，阈值为 22°C。"""

import numpy as np
import pytest

from services.chronic_risk_service import ChronicRiskService


USER = {'age': 70, 'chronic_diseases': ['高血压']}
HOT_NIGHT_PHRASE = '预计夜间最低温度为'
MISLEADING_PHRASE = '夜间温度>'


@pytest.fixture
def chronic_service(monkeypatch):
    class FakeDLNMService:
        def calculate_rr(
            self,
            temperature,
            lag_temperatures=None,
            disease_type=None,
            age=None,
        ):
            del temperature, lag_temperatures, disease_type, age
            return 1.0, {
                'raw_dlnm_rr': 1.0,
                'dlnm_disease_modifier': 1.0,
                'dlnm_age_modifier': 1.0,
                'uncapped_final_rr': 1.0,
                'dlnm_adjusted_rr': 1.0,
                'rr_cap': 3.5,
                'rr_cap_applied': False,
                'calculation_branch': 'test_stub',
            }

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: FakeDLNMService(),
    )
    return ChronicRiskService()


def _predict(service, weather_data):
    return service.predict_individual_risk(
        USER,
        weather_data,
        target_diseases=['cardiovascular'],
    )


def _rule_ids(result):
    return [
        item.get('rule_id')
        for item in result.get('triggered_rules') or []
    ]


def _advice_blob(result):
    texts = []
    for item in result.get('recommendations') or []:
        if isinstance(item, dict) and item.get('advice'):
            texts.append(str(item['advice']))
    for reason in (result.get('explain') or {}).get('reasons') or []:
        texts.append(str(reason))
    return '\n'.join(texts)


def test_temperature_min_22_triggers_without_tmin(chronic_service):
    result = _predict(
        chronic_service,
        {'temperature': 28, 'temperature_min': 22.0},
    )
    assert 'heat_night' in _rule_ids(result)
    assert HOT_NIGHT_PHRASE in _advice_blob(result)


def test_temperature_min_21_9_does_not_trigger(chronic_service):
    result = _predict(
        chronic_service,
        {'temperature': 28, 'temperature_min': 21.9},
    )
    assert 'heat_night' not in _rule_ids(result)


def test_legacy_tmin_22_still_triggers(chronic_service):
    result = _predict(chronic_service, {'temperature': 28, 'tmin': 22.0})
    assert 'heat_night' in _rule_ids(result)


def test_invalid_temperature_min_falls_back_to_valid_tmin(chronic_service):
    weather_data = {
        'temperature': 28,
        'temperature_min': 'n/a',
        'tmin': 23.0,
    }
    assert ChronicRiskService._parse_night_temperature(
        weather_data
    ) == pytest.approx(23.0)
    assert 'heat_night' in _rule_ids(_predict(chronic_service, weather_data))


def test_temperature_min_takes_priority_over_legacy_tmin(chronic_service):
    weather_data = {
        'temperature': 28,
        'temperature_min': 21.9,
        'tmin': 23.0,
    }
    assert ChronicRiskService._parse_night_temperature(
        weather_data
    ) == pytest.approx(21.9)
    assert 'heat_night' not in _rule_ids(
        _predict(chronic_service, weather_data)
    )


@pytest.mark.parametrize(
    'weather_data',
    [
        {'temperature': 28},
        {'temperature': 28, 'temperature_min': None, 'tmin': None},
        {'temperature': 28, 'temperature_min': 'n/a'},
        {'temperature': 28, 'tmin': 'n/a'},
        {'temperature': 28, 'temperature_min': float('nan')},
        {'temperature': 28, 'tmin': np.nan},
    ],
)
def test_missing_or_invalid_night_temp_does_not_trigger(
    chronic_service,
    weather_data,
):
    assert ChronicRiskService._parse_night_temperature(weather_data) is None
    result = _predict(chronic_service, weather_data)
    assert 'heat_night' not in _rule_ids(result)
    assert '热夜' not in _advice_blob(result)


def test_triggered_copy_uses_actual_min_temp_not_greater_than(
    chronic_service,
):
    result = _predict(
        chronic_service,
        {'temperature': 30, 'temperature_min': 23.5},
    )
    blob = _advice_blob(result)
    assert HOT_NIGHT_PHRASE in blob
    assert '23.5' in blob
    assert MISLEADING_PHRASE not in blob
    assert (
        chronic_service.recommendation_rules['heat_night']['thresholds'][
            'hot_night_temp'
        ]
        == '>=22'
    )


def test_safe_context_does_not_fake_missing_hot_night_temp(chronic_service):
    safe = chronic_service._build_safe_context({'hot_night': False})
    assert safe['hot_night'] is False
    assert safe['hot_night_temp'] is None
