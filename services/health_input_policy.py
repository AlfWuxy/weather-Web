# -*- coding: utf-8 -*-
"""健康评估输入边界：未知不能转换成正常值。"""
import json
import math

from services.missing_policy import input_state


def health_input_states(profile, weather, screening=None):
    """保留数据来源；插补数据不承担个体健康分级。"""
    states = {}
    for field, low, high, source in (
        ('age', 1, 150, profile), ('temperature', -80, 65, weather),
        ('aqi', 0, 500, weather), ('humidity', 0, 100, weather),
    ):
        raw = source.get(field)
        try:
            value = float(raw) if not isinstance(raw, bool) else None
            if value is not None and (not math.isfinite(value) or not low <= value <= high):
                value = None
        except (TypeError, ValueError):
            value = None
        metadata = (source.get('input_states') or {}).get(field, {})
        if source is weather and (source.get('is_mock') or source.get('is_demo')):
            value = None
            metadata = {'reason': 'non_observational_weather'}
        elif source is weather and field in (source.get('imputed_fields') or []):
            metadata = {**metadata, 'status': 'imputed'}
        if metadata.get('status') == 'imputed' and not metadata.get('method'):
            # 只有插补标记而无方法不能伪造来源说明。
            value = None
            metadata = {'status': 'unknown', 'reason': 'imputation_method_missing'}
        states[field] = input_state(
            value, source=metadata.get('source', 'submitted'),
            status=metadata.get('status') if value is not None else 'unknown',
            method=metadata.get('method'), reason=metadata.get('reason'),
        )
    diseases = profile.get('chronic_diseases')
    if isinstance(diseases, str):
        try:
            diseases = json.loads(diseases)
        except (ValueError, TypeError):
            diseases = None
    if not isinstance(diseases, list):
        diseases = None
    states['chronic_diseases'] = input_state(diseases, source='submitted')
    if screening is not None:
        choices = {
            'outdoor_exposure': {'low', 'medium', 'high'},
            'symptom_level': {'none', 'mild', 'moderate', 'severe'},
            'hydration': {'good', 'normal', 'poor'},
            'medication_adherence': {'good', 'partial', 'poor'},
            'sleep_quality': {'good', 'fair', 'poor'},
        }
        for field, allowed in choices.items():
            value = screening.get(field)
            states[field] = input_state(value if value in allowed else None, source='submitted')
    return states


def health_input_quality(states):
    """完整度不是置信度；插补或未知均须核实后才提供分级。"""
    unresolved = [name for name, state in states.items() if state['status'] != 'observed']
    total = len(states)
    return {
        'status': 'unknown' if unresolved else 'complete',
        'input_states': states,
        'missing_fields': unresolved,
        'completeness': round(100 * (total - len(unresolved)) / total) if total else 0,
        'requires_followup': bool(unresolved),
        'followup_priority': 'medium' if unresolved else None,
        'reason': '资料不足或含插补，风险未知；请补充资料并回访。' if unresolved else None,
    }


def unknown_health_result(quality):
    """未知输出与照护优先级分开，避免伪造中风险分数。"""
    return {
        'status': 'unknown', 'risk_score': None, 'risk_level': '风险未知',
        'overall_risk': {'score': None, 'rr': None, 'level': '风险未知', 'color': 'secondary'},
        'data_quality': quality, 'completeness': quality['completeness'],
        'input_states': quality['input_states'], 'requires_followup': True,
        'followup_priority': 'medium', 'risk_probabilities': None,
        'risk_interval': {}, 'disease_risks': {}, 'model_paths': [],
        'recommendations': [{'category': '资料核实', 'priority': 'medium', 'advice': quality['reason']}],
        'explain': {'reasons': [quality['reason']], 'actions': ['补充资料并安排回访'], 'escalation': []},
    }
