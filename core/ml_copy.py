# -*- coding: utf-8 -*-
"""Load ML recommendation copy from versioned JSON."""
import json
from functools import lru_cache
from pathlib import Path

_COPY_PATH = Path(__file__).resolve().parents[1] / 'data' / 'content' / 'ml_recommendation_copy.json'
_PERSONAL_KEYS = (
    'elderly', 'child', 'cold', 'chilly', 'hot_extreme', 'hot',
    'humid', 'dry', 'aqi_high', 'aqi_moderate', 'windy',
    'respiratory', 'digestive', 'cardio', 'routine',
)
_COMMUNITY_KEYS = (
    'high_aging', 'cold', 'cool', 'hot_extreme', 'hot',
    'aqi_high', 'aqi_moderate', 'humid', 'respiratory', 'digestive', 'routine',
)


@lru_cache(maxsize=1)
def load_ml_recommendation_copy():
    payload = json.loads(_COPY_PATH.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('ml_recommendation_copy.json must be an object')
    personal = payload.get('personal')
    community = payload.get('community')
    if not isinstance(personal, dict) or not isinstance(community, dict):
        raise ValueError('ml_recommendation_copy.json needs personal and community objects')
    missing_personal = [key for key in _PERSONAL_KEYS if not isinstance(personal.get(key), dict)]
    missing_community = [
        key for key in _COMMUNITY_KEYS
        if not isinstance(community.get(key), list) or not community[key]
    ]
    if missing_personal:
        raise ValueError(f'ml_recommendation_copy.json personal missing: {", ".join(missing_personal)}')
    if missing_community:
        raise ValueError(f'ml_recommendation_copy.json community missing: {", ".join(missing_community)}')
    if not personal['routine'].get('advice'):
        raise ValueError('ml_recommendation_copy.json personal.routine.advice required')
    return payload


def _item_from_copy(entry, *, advice=None, priority=None):
    if not isinstance(entry, dict):
        return None
    text = advice if advice is not None else entry.get('advice')
    if not text:
        return None
    return {
        'category': entry.get('category') or '日常健康',
        'advice': text,
        'priority': priority or entry.get('priority') or 'low',
    }


def generate_ml_personal_recommendations(age, top_predictions, metrics):
    copy = load_ml_recommendation_copy()['personal']
    recommendations = []
    if age >= 65:
        item = _item_from_copy(copy['elderly'])
        if item:
            recommendations.append(item)
    elif age < 10:
        item = _item_from_copy(copy['child'])
        if item:
            recommendations.append(item)

    temp = (metrics or {}).get('temp')
    humidity = (metrics or {}).get('humidity')
    aqi = (metrics or {}).get('aqi')
    wind_speed = (metrics or {}).get('wind_speed')

    if temp is not None and temp < 5:
        item = _item_from_copy(copy['cold'])
        if item:
            recommendations.append(item)
    elif temp is not None and temp < 10:
        item = _item_from_copy(copy['chilly'])
        if item:
            recommendations.append(item)
    elif temp is not None and temp > 35:
        item = _item_from_copy(copy['hot_extreme'])
        if item:
            recommendations.append(item)
    elif temp is not None and temp > 30:
        item = _item_from_copy(copy['hot'])
        if item:
            recommendations.append(item)

    if humidity is not None and humidity > 85:
        item = _item_from_copy(copy['humid'])
        if item:
            recommendations.append(item)
    elif humidity is not None and humidity < 40:
        item = _item_from_copy(copy['dry'])
        if item:
            recommendations.append(item)

    if aqi is not None and aqi > 150:
        item = _item_from_copy(copy['aqi_high'])
        if item:
            recommendations.append(item)
    elif aqi is not None and aqi > 100:
        item = _item_from_copy(copy['aqi_moderate'])
        if item:
            recommendations.append(item)

    if wind_speed is not None and wind_speed > 8:
        item = _item_from_copy(copy['windy'])
        if item:
            recommendations.append(item)

    for pred in top_predictions or []:
        disease = pred.get('disease') if isinstance(pred, dict) else None
        if not disease:
            continue
        probability = pred.get('probability') or 0
        priority = 'high' if probability > 0.3 else 'medium'
        if '呼吸' in disease or '支气管' in disease or '肺' in disease:
            entry = copy['respiratory']
        elif '胃' in disease or '肠' in disease or '消化' in disease:
            entry = copy['digestive']
        elif '高血压' in disease or '心血管' in disease:
            entry = copy['cardio']
        else:
            continue
        template = entry.get('advice_template') or entry.get('advice') or ''
        item = _item_from_copy(entry, advice=template.format(disease=disease), priority=priority)
        if item:
            recommendations.append(item)

    item = _item_from_copy(copy['routine'])
    if item:
        recommendations.append(item)

    priority_order = {'high': 0, 'medium': 1, 'low': 2}
    recommendations.sort(key=lambda row: priority_order.get(row.get('priority', 'low'), 2))
    return recommendations


def generate_ml_community_recommendations(elderly_ratio, metrics, disease_risks):
    copy = load_ml_recommendation_copy()['community']
    recommendations = []
    if elderly_ratio > 0.3:
        recommendations.extend(copy['high_aging'])

    if metrics:
        temp = metrics.get('temp')
        aqi = metrics.get('aqi')
        humidity = metrics.get('humidity')
        if temp is not None and temp < 5:
            recommendations.extend(copy['cold'])
        elif temp is not None and temp < 10:
            recommendations.extend(copy['cool'])
        if temp is not None and temp > 35:
            recommendations.extend(copy['hot_extreme'])
        elif temp is not None and temp > 32:
            recommendations.extend(copy['hot'])
        if aqi is not None and aqi > 150:
            recommendations.extend(copy['aqi_high'])
        elif aqi is not None and aqi > 100:
            recommendations.extend(copy['aqi_moderate'])
        if humidity is not None and humidity > 85:
            recommendations.extend(copy['humid'])

    if disease_risks:
        top_diseases = [item[0] for item in disease_risks[:3]]
        if any('呼吸' in name or '支气管' in name for name in top_diseases):
            recommendations.extend(copy['respiratory'])
        if any('胃' in name or '肠' in name for name in top_diseases):
            recommendations.extend(copy['digestive'])

    if not recommendations:
        recommendations.extend(copy['routine'])
    return recommendations
