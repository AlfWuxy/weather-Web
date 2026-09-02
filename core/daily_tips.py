# -*- coding: utf-8 -*-
"""Load caregiver daily action tips from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_TIPS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'daily_action_tips.json'


@lru_cache(maxsize=1)
def load_daily_action_tips():
    payload = json.loads(_TIPS_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('daily_action_tips.json must be an object')
    return payload


def action_plan_for_risk(risk_label):
    tips = load_daily_action_tips()
    plan = tips.get(risk_label)
    if not plan:
        plan = tips.get('低风险') or []
    return [dict(item) for item in plan]
