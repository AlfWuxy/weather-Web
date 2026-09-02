# -*- coding: utf-8 -*-
"""Load community-risk action tips from versioned JSON."""
import json
import math
from functools import lru_cache
from pathlib import Path

_TIPS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'community_action_tips.json'
_REQUIRED_KEYS = (
    'heading', 'many_high_risk', 'high_aging', 'hot', 'cold', 'extra_visits', 'routine', 'equity'
)
_EQUITY_KEYS = ('heat', 'data_gap', 'routine')


@lru_cache(maxsize=1)
def load_community_action_tips():
    payload = json.loads(_TIPS_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('community_action_tips.json must be an object')
    missing = [key for key in _REQUIRED_KEYS if not payload.get(key)]
    if missing:
        raise ValueError(f'community_action_tips.json missing: {", ".join(missing)}')
    equity = payload.get('equity')
    if not isinstance(equity, dict):
        raise ValueError('community_action_tips.json equity must be an object')
    equity_missing = [
        key for key in _EQUITY_KEYS
        if not (isinstance(equity.get(key), dict) and equity[key].get('advice'))
    ]
    if equity_missing:
        raise ValueError(f'community_action_tips.json equity missing: {", ".join(equity_missing)}')
    return payload


def _safe_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _tip_from_copy(entry, **template_values):
    if not isinstance(entry, dict):
        return None
    text = entry.get('advice')
    template = entry.get('advice_template')
    if text is None and template:
        text = template.format(**template_values)
    if not text:
        return None
    return {
        'category': entry.get('category') or '日常',
        'priority': entry.get('priority') or 'low',
        'advice': text,
        'target_communities': list(template_values.get('target_communities') or []),
    }


def generate_community_action_tips(high_risk_communities, weather_data):
    """Build caregiver-facing community tips; skip clinic staffing language."""
    copy = load_community_action_tips()
    communities = [row for row in (high_risk_communities or []) if isinstance(row, dict)]
    suggestions = []
    names = [row.get('community') for row in communities if row.get('community')]

    if len(communities) >= 3:
        tip = _tip_from_copy(
            copy['many_high_risk'],
            communities='、'.join(names[:2]),
            target_communities=names[:3],
        )
        if tip:
            suggestions.append(tip)

    for row in communities[:3]:
        ratio = _safe_float(row.get('elderly_ratio'))
        name = row.get('community')
        if ratio is None or ratio <= 0.4 or not name:
            continue
        tip = _tip_from_copy(
            copy['high_aging'],
            community=name,
            elderly_pct=f'{ratio * 100:.0f}',
            target_communities=[name],
        )
        if tip:
            suggestions.append(tip)

    temp = _safe_float((weather_data or {}).get('temperature') if isinstance(weather_data, dict) else None)
    if temp is not None and temp > 32:
        tip = _tip_from_copy(copy['hot'], target_communities=names)
        if tip:
            suggestions.append(tip)
    elif temp is not None and temp < 5:
        tip = _tip_from_copy(copy['cold'], target_communities=names)
        if tip:
            suggestions.append(tip)

    excess_values = [_safe_float(row.get('expected_excess_visits')) for row in communities]
    total_excess = sum(value for value in excess_values if value is not None)
    if total_excess > 10:
        tip = _tip_from_copy(copy['extra_visits'], target_communities=names)
        if tip:
            suggestions.append(tip)

    if not suggestions:
        tip = _tip_from_copy(copy['routine'])
        if tip:
            suggestions.append(tip)

    return suggestions


def equity_recommended_action(row):
    """Caregiver-facing equity action; skip clinic staffing language."""
    copy = load_community_action_tips()['equity']
    try:
        heat_level = int((row or {}).get('heatrisk_level') or 0)
    except (TypeError, ValueError):
        heat_level = 0
    uncertainty = _safe_float((row or {}).get('uncertainty_index')) or 0.0
    if heat_level >= 3:
        entry = copy['heat']
    elif uncertainty >= 70:
        entry = copy['data_gap']
    else:
        entry = copy['routine']
    return entry.get('advice') or ''
