# -*- coding: utf-8 -*-
"""Load today/elder dashboard copy from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_COPY_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'dashboard_copy.json'
_SECTIONS = ('today', 'elder')
_HEADLINE_KEYS = ('high', 'medium', 'low', 'weather_unavailable', 'unknown')
_EMPTY_PLAN_KEYS = ('title', 'detail')


@lru_cache(maxsize=1)
def load_dashboard_copy():
    payload = json.loads(_COPY_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('dashboard_copy.json must be an object')
    copy = {}
    for section in _SECTIONS:
        raw = payload.get(section)
        if not isinstance(raw, dict):
            raise ValueError(f'dashboard_copy.json missing section: {section}')
        headlines = raw.get('headlines')
        empty_plan = raw.get('empty_plan')
        if not isinstance(headlines, dict):
            raise ValueError(f'dashboard_copy.json {section}.headlines must be an object')
        if not isinstance(empty_plan, dict):
            raise ValueError(f'dashboard_copy.json {section}.empty_plan must be an object')
        missing_headlines = [key for key in _HEADLINE_KEYS if not headlines.get(key)]
        missing_plan = [key for key in _EMPTY_PLAN_KEYS if not empty_plan.get(key)]
        if missing_headlines:
            raise ValueError(
                f'dashboard_copy.json {section}.headlines missing: {", ".join(missing_headlines)}'
            )
        if missing_plan:
            raise ValueError(
                f'dashboard_copy.json {section}.empty_plan missing: {", ".join(missing_plan)}'
            )
        copy[section] = {
            'headlines': {key: headlines[key] for key in _HEADLINE_KEYS},
            'empty_plan': {key: empty_plan[key] for key in _EMPTY_PLAN_KEYS},
        }
    return copy


def select_dashboard_headline(copy, *, section, risk_level, weather_available):
    headlines = ((copy or {}).get(section) or {}).get('headlines') or {}
    if not weather_available:
        return headlines.get('weather_unavailable') or ''
    if risk_level in ('high', 'extreme'):
        return headlines.get('high') or ''
    if risk_level == 'medium':
        return headlines.get('medium') or ''
    if risk_level == 'low':
        return headlines.get('low') or ''
    return headlines.get('unknown') or ''
