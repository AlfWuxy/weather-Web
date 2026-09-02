# -*- coding: utf-8 -*-
"""Load caregiver daily action tips from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_TIPS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'daily_action_tips.json'

HEAT_RISK_LABELS = {
    'low': '低风险',
    'medium': '中风险',
    'high': '高风险',
    'extreme': '极高',
}
UNKNOWN_HEAT_RISK_LABEL = '风险未知'


@lru_cache(maxsize=1)
def load_daily_action_tips():
    payload = json.loads(_TIPS_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('daily_action_tips.json must be an object')
    return payload


def label_for_heat_level(risk_level):
    if not risk_level:
        return UNKNOWN_HEAT_RISK_LABEL
    return HEAT_RISK_LABELS.get(risk_level, UNKNOWN_HEAT_RISK_LABEL)


def action_plan_for_risk(risk_label):
    if not risk_label or risk_label == UNKNOWN_HEAT_RISK_LABEL:
        return []
    tips = load_daily_action_tips()
    plan = tips.get(risk_label) or []
    return [dict(item) for item in plan]
