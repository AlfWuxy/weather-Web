# -*- coding: utf-8 -*-
"""Shared helpers for rendering QWeather-based 7-day health forecasts."""
from datetime import datetime

from utils.parsers import parse_float


def score_level(score):
    """按分值映射页面展示等级。"""
    if score >= 70:
        return '高风险'
    if score >= 40:
        return '中等风险'
    return '低风险'


def level_bucket(score):
    """按分值映射条形图样式。"""
    if score >= 70:
        return 'high'
    if score >= 40:
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
    if parsed is None:
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
    health_by_date = {
        item.get('date'): item
        for item in (health_forecasts or [])
        if isinstance(item, dict) and item.get('date')
    }
    cards = []
    for entry in qweather_days or []:
        if not isinstance(entry, dict):
            continue
        day = forecast_date(entry.get('date') or entry.get('forecast_date'))
        if not day:
            continue
        dow, date_label = forecast_day_labels(day, start_date)
        health = health_by_date.get(day.strftime('%Y-%m-%d'), {})
        composite = health.get('composite_exposure') or {}
        score = parse_float(composite.get('score'))
        if score is None:
            score = parse_float(health.get('probability_high_visits'))
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
        })
    return cards
