# -*- coding: utf-8 -*-
"""健康评估建议面向照护，并从 JSON 读取。"""


def _compose(weather, cap, matrix, profile=None, screening=None, chronic=None):
    from services.health_risk_service import HealthRiskService

    return HealthRiskService()._compose_recommendations(
        chronic or [],
        cap,
        matrix,
        weather,
        profile or {},
        screening or {},
    )


def test_health_routine_recommendation_comes_from_json():
    from core.health_copy import load_health_assessment_tips

    load_health_assessment_tips.cache_clear()
    copy = load_health_assessment_tips()
    recs = _compose(
        {'temperature': 22, 'aqi': 40},
        {'urgency': 'future'},
        {'matrix_score': 1},
    )

    assert copy['routine']['advice'] in {item['advice'] for item in recs}


def test_health_escalate_copy_does_not_ask_village_doctor():
    recs = _compose(
        {'temperature': 22, 'aqi': 40},
        {'urgency': 'future'},
        {'matrix_score': 12},
    )
    text = ' '.join(item.get('advice', '') for item in recs)
    assert recs
    assert '村医' not in text
    assert '联系家属' in text


def test_health_notification_copy_does_not_ask_village_doctor():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath(
        'services', 'user', 'profile_service.py'
    ).read_text(encoding='utf-8')
    assert '村医' not in source


def test_health_hot_recommendation_comes_from_json():
    from core.health_copy import load_health_assessment_tips

    load_health_assessment_tips.cache_clear()
    copy = load_health_assessment_tips()
    recs = _compose(
        {'temperature': 34, 'aqi': 40},
        {'urgency': 'future'},
        {'matrix_score': 1},
    )
    assert copy['hot']['advice'] in {item['advice'] for item in recs}
