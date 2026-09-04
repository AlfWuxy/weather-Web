# -*- coding: utf-8 -*-
"""紧急分诊提示面向家属，不编造村医排班。"""


def test_emergency_triage_does_not_ask_village_doctor():
    from services.emergency_triage import triage_symptoms

    result = triage_symptoms('胸痛')
    text = ' '.join(result.get('actions') or [])
    assert result['is_emergency'] is True
    assert '村医' not in text
    assert '家属' in text
    assert '120' in text
