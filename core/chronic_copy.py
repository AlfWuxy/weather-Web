# -*- coding: utf-8 -*-
"""Load chronic-risk recommendation copy from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_COPY_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'chronic_recommendation_copy.json'
_RULE_IDS = (
    'heat_high_rr',
    'heat_night',
    'heat_wave',
    'cold_high_rr',
    'cold_wave',
    'aqi_high',
    'aqi_moderate',
    'elderly_extreme_weather',
    'comorbidity_risk',
    'medication_reminder',
)
_RULE_FIELDS = (
    'name', 'priority', 'category', 'thresholds', 'context_fields',
    'reason_template', 'template', 'diseases',
)


@lru_cache(maxsize=1)
def load_chronic_recommendation_copy():
    payload = json.loads(_COPY_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('chronic_recommendation_copy.json must be an object')
    rules = payload.get('rules')
    if not isinstance(rules, dict):
        raise ValueError('chronic_recommendation_copy.json rules must be an object')
    missing_rules = []
    for rule_id in _RULE_IDS:
        rule = rules.get(rule_id)
        if not isinstance(rule, dict) or any(not rule.get(field) and rule.get(field) is not False for field in _RULE_FIELDS):
            # thresholds/hot_night can be True; template must be present
            if not isinstance(rule, dict) or not rule.get('template') or not rule.get('name'):
                missing_rules.append(rule_id)
    if missing_rules:
        raise ValueError(
            f'chronic_recommendation_copy.json rules missing: {", ".join(missing_rules)}'
        )
    escalation = payload.get('escalation')
    if not isinstance(escalation, dict) or not escalation.get('family_help'):
        raise ValueError('chronic_recommendation_copy.json missing escalation.family_help')
    if not payload.get('default_advice'):
        raise ValueError('chronic_recommendation_copy.json missing default_advice')
    fallback = payload.get('fallback_actions')
    if not isinstance(fallback, list) or not fallback:
        raise ValueError('chronic_recommendation_copy.json missing fallback_actions')
    return payload
