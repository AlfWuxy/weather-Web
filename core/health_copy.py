# -*- coding: utf-8 -*-
"""Load health-assessment action tips from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_TIPS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'health_assessment_tips.json'
_REQUIRED_KEYS = (
    'hot', 'cold', 'aqi', 'symptom', 'medication', 'emergency', 'escalate', 'routine'
)


@lru_cache(maxsize=1)
def load_health_assessment_tips():
    payload = json.loads(_TIPS_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('health_assessment_tips.json must be an object')
    missing = [
        key for key in _REQUIRED_KEYS
        if not isinstance(payload.get(key), dict) or not payload[key].get('advice')
    ]
    if missing:
        raise ValueError(f'health_assessment_tips.json missing: {", ".join(missing)}')
    return payload
