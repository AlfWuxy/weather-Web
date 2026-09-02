# -*- coding: utf-8 -*-
"""Load 7-day weekly caregiver tips from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_TIPS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'forecast_week_tips.json'
_REQUIRED_KEYS = ('high_visit_days', 'temperature_swing', 'weekend_peak', 'routine')


@lru_cache(maxsize=1)
def load_forecast_week_tips():
    payload = json.loads(_TIPS_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('forecast_week_tips.json must be an object')
    missing = [key for key in _REQUIRED_KEYS if not payload.get(key)]
    if missing:
        raise ValueError(f'forecast_week_tips.json missing: {", ".join(missing)}')
    return payload


def _tip_from_copy(entry, *, advice=None, **template_values):
    if not isinstance(entry, dict):
        return None
    text = advice if advice is not None else entry.get('advice')
    template = entry.get('advice_template')
    if text is None and template:
        text = template.format(**template_values)
    if not text:
        return None
    return {
        'priority': entry.get('priority') or 'low',
        'category': entry.get('category') or '日常',
        'advice': text,
    }


def generate_forecast_week_tips(forecasts, high_risk_days):
    """Build caregiver-facing weekly tips; skip clinic staffing language."""
    copy = load_forecast_week_tips()
    recommendations = []

    if high_risk_days >= 3:
        tip = _tip_from_copy(copy['high_visit_days'], high_risk_days=high_risk_days)
        if tip:
            recommendations.append(tip)

    for day in forecasts or []:
        for event in day.get('extreme_events') or []:
            if not isinstance(event, dict):
                continue
            description = event.get('description')
            if not description:
                continue
            recommendations.append({
                'priority': 'high' if event.get('severity') == 'extreme' else 'medium',
                'category': '极端天气',
                'advice': f"{day.get('date')}: {description}",
            })

    temps = [
        day['temperature']['corrected']
        for day in forecasts or []
        if isinstance(day.get('temperature'), dict)
        and day['temperature'].get('corrected') is not None
    ]
    if len(temps) >= 2 and (max(temps) - min(temps) > 10):
        tip = _tip_from_copy(
            copy['temperature_swing'],
            min_temp=f'{min(temps):.0f}',
            max_temp=f'{max(temps):.0f}',
        )
        if tip:
            recommendations.append(tip)

    weekend_high = [
        day for day in forecasts or []
        if day.get('day_of_week') in ('周六', '周日')
        and day.get('risk_level') in ('红色预警', '橙色预警')
    ]
    if weekend_high:
        tip = _tip_from_copy(copy['weekend_peak'])
        if tip:
            recommendations.append(tip)

    if not recommendations:
        tip = _tip_from_copy(copy['routine'])
        if tip:
            recommendations.append(tip)

    return recommendations
