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


def _finite_float(value):
    """风险展示字段只接受有限数值。"""
    parsed = parse_float(value)
    if parsed is None or not math.isfinite(parsed):
        return None
    return parsed


def _dict_or_empty(value):
    return value if isinstance(value, dict) else {}


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
        for field in ('temperature_max', 'temperature_min', 'humidity', 'wind_speed'):
            value = parse_float(entry.get(field))
            if value is None or not math.isfinite(value):
                return []
        if not str(entry.get('condition') or '').strip():
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
        health = _dict_or_empty(health_by_date.get(day.strftime('%Y-%m-%d')))
        composite = _dict_or_empty(health.get('composite_exposure'))
        components = _dict_or_empty(composite.get('components'))
        composite_inputs = _dict_or_empty(composite.get('inputs'))
        temperature_input = _dict_or_empty(composite_inputs.get('temperature'))
        temp_min_input = _dict_or_empty(composite_inputs.get('temp_min'))
        humidity_input = _dict_or_empty(composite_inputs.get('humidity'))
        pm25_input = _dict_or_empty(composite_inputs.get('pm25'))
        visits = _dict_or_empty(health.get('visits'))
        predictability = _dict_or_empty(health.get('predictability'))
        predictability_inputs = _dict_or_empty(predictability.get('inputs'))
        score = _finite_float(composite.get('final_score'))
        if score is None:
            score = _finite_float(composite.get('score'))
        risk_available = score is not None
        if risk_available:
            score = max(0, min(100, int(round(score))))
        cards.append({
            'data_source': 'QWeather',
            'dow': dow,
            'date': date_label,
            'full_date': day.strftime('%Y-%m-%d'),
            'temp_high': forecast_temp(entry.get('temperature_max')),
            'temp_low': forecast_temp(entry.get('temperature_min')),
            'condition': entry.get('condition') or entry.get('condition_night') or '未知',
            'precip_probability': parse_float(entry.get('precip_probability')),
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


def build_weather_only_forecast_cards(openmeteo_days, start_date):
    """构造 Open-Meteo 天气卡，不生成或插补任何健康风险字段。"""
    cards = []
    for entry in list(openmeteo_days or []):
        if not isinstance(entry, dict):
            return []
        if entry.get('is_mock') or entry.get('is_demo') or entry.get('data_source') != 'Open-Meteo':
            return []
        day = forecast_date(entry.get('date') or entry.get('forecast_date'))
        tmax = forecast_temp(entry.get('temperature_max'))
        tmin = forecast_temp(entry.get('temperature_min'))
        precipitation = parse_float(entry.get('precip_probability'))
        condition = str(entry.get('condition') or '').strip()
        if (
            day is None
            or tmax is None
            or tmin is None
            or not -90 <= tmin <= 60
            or not -90 <= tmax <= 60
            or tmax < tmin
            or precipitation is None
            or not math.isfinite(precipitation)
            or not 0 <= precipitation <= 100
            or not condition
        ):
            return []
        dow, date_label = forecast_day_labels(day, start_date)
        cards.append({
            'data_source': 'Open-Meteo',
            'dow': dow,
            'date': date_label,
            'full_date': day.strftime('%Y-%m-%d'),
            'temp_high': tmax,
            'temp_low': tmin,
            'condition': condition,
            'precip_probability': round(precipitation, 1),
            'risk_level': 'unknown',
            'risk_score': None,
            'risk_label': '健康风险待计算',
            'risk_available': False,
            'risk_components': {
                'heat': None,
                'pm25': None,
                'humidity': None,
                'hot_night': None,
            },
            'composite_pre_clip_score': None,
            'composite_final_score': None,
            'composite_synergy_bonus': None,
            'temperature_used': None,
            'temperature_imputed': None,
            'temp_min_used': None,
            'temp_min_imputed': None,
            'temp_min_source': None,
            'humidity_used': None,
            'humidity_imputed': None,
            'humidity_source': None,
            'pm25_used': None,
            'pm25_imputed': None,
            'pm25_source': None,
            'pm25_detail_source': None,
            'pm25_aqi_used': None,
            'pm25_proxy': None,
            'probability_high_visits': None,
            'visit_point_estimate': None,
            'visit_raw_point_estimate': None,
            'visit_rr': None,
            'visit_baseline': None,
            'visit_dow_factor': None,
            'visit_threshold_p90': None,
            'visit_std_estimate': None,
            'visit_probability_method': None,
            'visit_guardrail_cap': None,
            'visit_guardrail_applied': None,
            'predictability_score': None,
            'predictability_label': None,
            'predictability_branch': None,
            'predictability_raw_score': None,
            'predictability_external_score': None,
            'predictability_lead_day': None,
            'predictability_model_spread': None,
            'predictability_model_count': None,
            'predictability_lead_penalty': None,
            'predictability_model_bonus': None,
        })
    return cards
