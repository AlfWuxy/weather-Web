# -*- coding: utf-8 -*-
"""Shared helpers for rendering QWeather-based 7-day health forecasts."""
from datetime import datetime
import math

from utils.parsers import parse_float


def score_level(score):
    """按分值映射页面展示等级。"""
    if score >= 70:
        return '高风险'
    if score >= 45:
        return '中等风险'
    return '低风险'


def level_bucket(score):
    """按分值映射条形图样式。"""
    if score >= 70:
        return 'high'
    if score >= 45:
        return 'mid'
    return 'low'


def forecast_date(value):
    """解析和风日期字段。"""
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except Exception:
        return None


def forecast_temp(value):
    """把温度转换成页面展示值，保留必要的小数。"""
    parsed = parse_float(value)
    if parsed is None or not math.isfinite(parsed):
        return None
    if float(parsed).is_integer():
        return int(parsed)
    return round(parsed, 1)


def forecast_day_labels(day, start_date):
    """生成卡片用的“今/明/周几”标签。"""
    delta = (day - start_date).days
    if delta == 0:
        return '今', '今天'
    if delta == 1:
        return '明', '明天'
    weekday = ['一', '二', '三', '四', '五', '六', '日'][day.weekday()]
    return weekday, f'周{weekday}'


def build_forecast_cards(qweather_days, health_forecasts, start_date):
    """把和风日预报与健康预测合并为模板卡片。"""
    entries = list(qweather_days or [])
    for entry in entries:
        if not isinstance(entry, dict):
            return []
        for field in ('temperature_max', 'temperature_min', 'humidity'):
            value = parse_float(entry.get(field))
            if value is None or not math.isfinite(value):
                return []

    health_by_date = {
        item.get('date'): item
        for item in (health_forecasts or [])
        if isinstance(item, dict) and item.get('date')
    }
    cards = []
    for entry in entries:
        day = forecast_date(entry.get('date') or entry.get('forecast_date'))
        if not day:
            continue
        dow, date_label = forecast_day_labels(day, start_date)
        health = health_by_date.get(day.strftime('%Y-%m-%d'), {})
        composite = health.get('composite_exposure') or {}
        components = composite.get('components') or {}
        composite_inputs = composite.get('inputs') or {}
        temperature_input = composite_inputs.get('temperature') or {}
        temp_min_input = composite_inputs.get('temp_min') or {}
        humidity_input = composite_inputs.get('humidity') or {}
        pm25_input = composite_inputs.get('pm25') or {}
        visits = health.get('visits') or {}
        predictability = health.get('predictability') or {}
        predictability_inputs = predictability.get('inputs') or {}
        score = parse_float(composite.get('final_score'))
        if score is None:
            score = parse_float(composite.get('score'))
        risk_available = score is not None
        if risk_available:
            score = max(0, min(100, int(round(score))))
        cards.append({
            'dow': dow,
            'date': date_label,
            'full_date': day.strftime('%Y-%m-%d'),
            'temp_high': forecast_temp(entry.get('temperature_max')),
            'temp_low': forecast_temp(entry.get('temperature_min')),
            'condition': entry.get('condition') or entry.get('condition_night') or '未知',
            'risk_level': level_bucket(score) if risk_available else 'unknown',
            'risk_score': score,
            'risk_label': score_level(score) if risk_available else '待计算',
            'risk_available': risk_available,
            'risk_components': {
                'heat': parse_float(components.get('heat')),
                'pm25': parse_float(components.get('pm25')),
                'humidity': parse_float(components.get('humidity')),
                'hot_night': parse_float(components.get('hot_night')),
            },
            'composite_pre_clip_score': parse_float(composite.get('pre_clip_score')),
            'composite_final_score': parse_float(composite.get('final_score', composite.get('score'))),
            'composite_synergy_bonus': parse_float(composite.get('synergy_bonus')),
            'temperature_used': parse_float(temperature_input.get('used_value')),
            'temperature_imputed': temperature_input.get('imputed'),
            'temp_min_used': parse_float(temp_min_input.get('used_value')),
            'temp_min_imputed': temp_min_input.get('imputed'),
            'temp_min_source': temp_min_input.get('source'),
            'humidity_used': parse_float(humidity_input.get('used_value')),
            'humidity_imputed': humidity_input.get('imputed'),
            'humidity_source': humidity_input.get('source'),
            'pm25_used': parse_float(pm25_input.get('used_value')),
            'pm25_imputed': pm25_input.get('imputed'),
            'pm25_source': composite.get('pm25_source') or pm25_input.get('source'),
            'pm25_detail_source': pm25_input.get('detail_source'),
            'pm25_aqi_used': parse_float(pm25_input.get('aqi_used')),
            'pm25_proxy': parse_float(composite.get('pm25_proxy')),
            'probability_high_visits': parse_float(health.get('probability_high_visits')),
            'visit_point_estimate': parse_float(visits.get('point_estimate')),
            'visit_raw_point_estimate': parse_float(visits.get('raw_point_estimate')),
            'visit_rr': parse_float(visits.get('rr')),
            'visit_baseline': parse_float(visits.get('baseline')),
            'visit_dow_factor': parse_float(visits.get('dow_factor')),
            'visit_threshold_p90': parse_float(visits.get('visit_threshold_p90')),
            'visit_std_estimate': parse_float(visits.get('std_estimate')),
            'visit_probability_method': visits.get('probability_method'),
            'visit_guardrail_cap': parse_float(visits.get('guardrail_cap')),
            'visit_guardrail_applied': visits.get('guardrail_applied'),
            'predictability_score': parse_float(predictability.get('score')),
            'predictability_label': predictability.get('label'),
            'predictability_branch': predictability.get('branch'),
            'predictability_raw_score': parse_float(predictability.get('raw_score')),
            'predictability_external_score': parse_float(predictability_inputs.get('external_score')),
            'predictability_lead_day': predictability_inputs.get('lead_day'),
            'predictability_model_spread': parse_float(predictability_inputs.get('model_spread')),
            'predictability_model_count': predictability_inputs.get('model_count'),
            'predictability_lead_penalty': parse_float(predictability_inputs.get('lead_penalty')),
            'predictability_model_bonus': parse_float(predictability_inputs.get('model_bonus')),
        })
    return cards
