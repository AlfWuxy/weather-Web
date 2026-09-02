# -*- coding: utf-8 -*-
"""ML 建议面向照护，不编造巡访排班，并从 JSON 读取。"""

from services.ml_prediction_service import MLPredictionService


def _service():
    return object.__new__(MLPredictionService)


def test_ml_community_recs_are_caregiver_copy_not_clinic_staffing():
    recs = _service()._generate_community_recommendations(
        0.42,
        {'temperature': 36, 'humidity': 90, 'aqi': 160},
        [('支气管炎', 0.4)],
    )
    text = ' '.join(recs)
    assert recs
    assert '巡访' not in text
    assert '卫生站' not in text
    assert '常规健康管理' not in text
    assert '红色预警' not in text
    assert '暖心驿站' not in text
    assert any('提醒' in item or '家人' in item or '避暑' in item for item in recs)


def test_ml_community_routine_comes_from_json():
    from core.ml_copy import load_ml_recommendation_copy

    load_ml_recommendation_copy.cache_clear()
    copy = load_ml_recommendation_copy()
    recs = _service()._generate_community_recommendations(0.1, None, None)
    assert copy['community']['routine'][0] in recs


def test_ml_personal_routine_comes_from_json():
    from core.ml_copy import load_ml_recommendation_copy

    load_ml_recommendation_copy.cache_clear()
    copy = load_ml_recommendation_copy()
    recs = _service()._generate_recommendations(40, '男', [], {})
    assert copy['personal']['routine']['advice'] in {item['advice'] for item in recs}


def test_ml_service_does_not_hardcode_clinic_community_copy():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath(
        'services', 'ml_prediction_service.py'
    ).read_text(encoding='utf-8')
    assert '加强对独居老人的健康巡访' not in source
    assert '保持常规健康管理工作' not in source
    assert '社区卫生站做好应急药品储备' not in source
