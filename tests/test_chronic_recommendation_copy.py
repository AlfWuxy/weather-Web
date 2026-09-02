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


def test_vital_high_sbp_copy_is_caregiver_not_clinic_staffing():
    from services.chronic_risk_service import ChronicRiskService

    result = ChronicRiskService()._analyze_submitted_vitals({'sbp': 185})
    text = ' '.join(result.get('recommendations') or [])
    assert result['recommendations']
    assert '社区医生' not in text
    assert '就医' in text or '家属' in text


def test_vital_copy_comes_from_json():
    from core.chronic_copy import load_chronic_recommendation_copy
    from services.chronic_risk_service import ChronicRiskService

    load_chronic_recommendation_copy.cache_clear()
    copy = load_chronic_recommendation_copy()
    result = ChronicRiskService()._analyze_submitted_vitals({'sbp': 185, 'fbg': 12})
    assert copy['vitals']['sbp_very_high']['advice'] in result['recommendations']
    assert copy['vitals']['fbg_very_high']['advice'] in result['recommendations']
    assert any(
        copy['vitals']['sbp_very_high']['factor_template'].format(sbp=185) == item
        for item in result['factors']
    )


def test_chronic_service_does_not_hardcode_clinic_vital_copy():
    source = (_REPO_ROOT / 'services' / 'chronic_risk_service.py').read_text(encoding='utf-8')
    assert '建议尽快联系社区医生复核' not in source
