# -*- coding: utf-8 -*-
"""Load 7-day weekly caregiver tips from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_TIPS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'forecast_week_tips.json'
_REQUIRED_KEYS = ('high_visit_days', 'temperature_swing', 'weekend_peak', 'routine', 'role_cards')
_ROLE_CARD_KEYS = (
    'resident_daily',
    'resident_composite',
    'doctor_prepare',
    'doctor_followup',
    'community_cooling',
    'community_info',
)


@lru_cache(maxsize=1)
def load_forecast_week_tips():
    payload = json.loads(_TIPS_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('forecast_week_tips.json must be an object')
    missing = [key for key in _REQUIRED_KEYS if not payload.get(key)]
    if missing:
        raise ValueError(f'forecast_week_tips.json missing: {", ".join(missing)}')
    role_cards = payload.get('role_cards')
    if not isinstance(role_cards, dict):
        raise ValueError('forecast_week_tips.json role_cards must be an object')
    role_missing = [
        key for key in _ROLE_CARD_KEYS
        if not isinstance(role_cards.get(key), dict)
        or not (role_cards[key].get('action') or role_cards[key].get('action_template'))
        or not role_cards[key].get('title')
    ]
    if role_missing:
        raise ValueError(f'forecast_week_tips.json role_cards missing: {", ".join(role_missing)}')
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


def _safe_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _card_from_copy(entry, **template_values):
    if not isinstance(entry, dict):
        return None
    text = entry.get('action')
    template = entry.get('action_template')
    if text is None and template:
        text = template.format(**template_values)
    if not text or not entry.get('title'):
        return None
    return {
        'priority': entry.get('priority') or 'medium',
        'title': entry['title'],
        'action': text,
    }


def build_role_action_cards(forecasts, summary):
    """Keep resident/doctor/community keys; use caregiver voice from JSON."""
    copy = load_forecast_week_tips()['role_cards']
    days = [row for row in (forecasts or []) if isinstance(row, dict)]
    high_days = [
        row for row in days
        if (_safe_float(row.get('probability_high_visits'), 0.0) or 0.0) >= 50
    ]
    composite_high_days = [
        row for row in days
        if (row.get('composite_exposure') or {}).get('level') == '高'
    ]
    scenario = (summary or {}).get('scenario_totals') or {}
    baseline_total = _safe_float(scenario.get('baseline_total'), 0.0) or 0.0
    worst_total = _safe_float(scenario.get('worst_case_total'), baseline_total) or baseline_total
    extra = max(0.0, worst_total - baseline_total)

    resident_cards = []
    daily = _card_from_copy(copy['resident_daily'])
    if daily:
        daily['priority'] = 'high' if high_days else 'medium'
        resident_cards.append(daily)
    if composite_high_days:
        composite = _card_from_copy(copy['resident_composite'])
        if composite:
            composite['priority'] = 'high'
            resident_cards.append(composite)

    doctor_cards = []
    prepare = _card_from_copy(copy['doctor_prepare'], extra=round(extra, 1))
    if prepare:
        prepare['priority'] = 'high' if high_days else 'medium'
        doctor_cards.append(prepare)
    if any((row.get('cap_semantics') or {}).get('urgency') == 'immediate' for row in days):
        followup = _card_from_copy(copy['doctor_followup'])
        if followup:
            followup['priority'] = 'high'
            doctor_cards.append(followup)

    community_cards = []
    cooling = _card_from_copy(copy['community_cooling'])
    if cooling:
        cooling['priority'] = 'high' if len(high_days) >= 2 else 'medium'
        community_cards.append(cooling)
    info = _card_from_copy(copy['community_info'])
    if info:
        info['priority'] = 'medium'
        community_cards.append(info)

    return {
        'resident': resident_cards,
        'doctor': doctor_cards,
        'community': community_cards,
    }
