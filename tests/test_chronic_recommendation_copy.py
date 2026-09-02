# -*- coding: utf-8 -*-
"""慢病建议模板从 JSON 读取，升级提示不编造村医排班。"""
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_chronic_recommendation_templates_come_from_json():
    from core.chronic_copy import load_chronic_recommendation_copy
    from services.chronic_risk_service import ChronicRiskService

    load_chronic_recommendation_copy.cache_clear()
    copy = load_chronic_recommendation_copy()
    service = ChronicRiskService()

    assert service.recommendation_rules['heat_high_rr']['template'] == copy['rules']['heat_high_rr']['template']
    assert service.recommendation_rules['heat_night']['template'] == copy['rules']['heat_night']['template']
    assert service.recommendation_rules['heat_night']['thresholds']['hot_night_temp'] == '>=22'


def test_chronic_service_does_not_hardcode_heat_template():
    source = (_REPO_ROOT / 'services' / 'chronic_risk_service.py').read_text(encoding='utf-8')
    assert '高温天气({temperature}°C)下您的{disease_type}' not in source
    assert '建议及时联系家属或村医协助观察。' not in source


def test_chronic_escalation_does_not_ask_village_doctor():
    from core.chronic_copy import load_chronic_recommendation_copy
    from services.chronic_risk_service import ChronicRiskService

    load_chronic_recommendation_copy.cache_clear()
    copy = load_chronic_recommendation_copy()
    service = ChronicRiskService()
    explain, _triggered = service.build_explain(
        {'age': 80, 'disease_count': 2, 'rr': 1.1, 'heat_wave_days': 0, 'cold_wave_days': 0, 'aqi': 40},
        [],
    )

    text = ' '.join(explain.get('escalation') or [])
    assert copy['escalation']['family_help'] in (explain.get('escalation') or [])
    assert '村医' not in text
