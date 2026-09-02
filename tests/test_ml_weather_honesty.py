# -*- coding: utf-8 -*-
"""ML 建议和调权不得把缺测湿度/AQI 当成 70/50。"""

from services.ml_prediction_service import MLPredictionService


def _service():
    return object.__new__(MLPredictionService)


def test_recommendations_skip_missing_humidity_and_aqi():
    service = _service()
    recs = service._generate_recommendations(
        72,
        '未知',
        [{'disease': '高血压', 'probability': 0.2}],
        {
            'temperature': 31,
            'tmean': 31,
            'humidity': None,
            'aqi': None,
            'wind_speed': None,
        },
    )
    assert isinstance(recs, list)
    categories = [item.get('category') for item in recs]
    assert '空气质量警告' not in categories
    assert '空气质量提醒' not in categories
    assert '高湿度提醒' not in categories
    assert '干燥提醒' not in categories


def test_adjust_probability_skips_missing_humidity_instead_of_crashing():
    service = _service()
    service.disease_weather_sensitivity = {
        '支气管炎': {'high_humidity': 1.3, 'low_humidity': 1.3},
    }
    adjusted = service._adjust_probability_by_weather(
        '支气管炎',
        0.2,
        {'temperature': 20, 'tmean': 20, 'humidity': None},
    )
    assert adjusted == 0.2


def test_risk_score_does_not_invent_aqi_when_missing():
    service = _service()
    predictions = [{'probability': 0.1}]
    mild = {
        'temperature': 25,
        'tmean': 25,
        'humidity': 60,
        'aqi': None,
        'wind_speed': 2.0,
    }
    dirty = dict(mild)
    dirty['aqi'] = 180
    missing = service._calculate_risk_score(40, predictions, mild)
    polluted = service._calculate_risk_score(40, predictions, dirty)
    assert polluted > missing


def test_risk_score_keeps_real_temperature_when_aqi_is_invalid():
    service = _service()
    predictions = [{'probability': 0}]
    heat = service._calculate_risk_score(40, predictions, {
        'temperature': 39,
        'tmean': 39,
        'humidity': 60,
        'aqi': 'bad',
        'wind_speed': 2,
    })
    mild = service._calculate_risk_score(40, predictions, {
        'temperature': 20,
        'tmean': 20,
        'humidity': 70,
        'aqi': 50,
        'wind_speed': 2.5,
    })
    assert heat > mild
