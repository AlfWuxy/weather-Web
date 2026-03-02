# -*- coding: utf-8 -*-
"""Health profile helpers."""
import json
import logging
import re

from utils.parsers import (
    parse_int,
    parse_float,
    safe_json_loads,
    compact_dict,
    json_or_none
)
from utils.validators import sanitize_input

logger = logging.getLogger(__name__)

def _parse_chronic_diseases_from_form(form):
    """解析慢病列表（含自由输入）"""
    chronic_diseases = [sanitize_input(d, max_length=50) for d in form.getlist('chronic_diseases') if d]
    chronic_other = sanitize_input(form.get('chronic_disease_other'), max_length=50)
    if chronic_other:
        for item in re.split(r'[，,、\s]+', chronic_other):
            item = sanitize_input(item, max_length=50)
            if item:
                chronic_diseases.append(item)
    return list(dict.fromkeys(chronic_diseases))


def _build_member_profile_form_payload(form):
    """构建家庭成员画像字段"""
    allergies = sanitize_input(form.get('allergies'), max_length=200)
    medications = sanitize_input(form.get('medications'), max_length=200)
    risk_tags = [sanitize_input(t, max_length=50) for t in form.getlist('risk_tags') if t]
    risk_tags = list(dict.fromkeys([t for t in risk_tags if t]))

    metrics = {}
    bp_sys = parse_int(form.get('bp_sys'))
    bp_dia = parse_int(form.get('bp_dia'))
    if bp_sys and bp_dia:
        metrics['blood_pressure'] = f"{bp_sys}/{bp_dia}"
    blood_sugar = parse_float(form.get('blood_sugar'))
    if blood_sugar is not None:
        metrics['blood_sugar'] = blood_sugar
    heart_rate = parse_int(form.get('heart_rate'))
    if heart_rate is not None:
        metrics['heart_rate'] = heart_rate
    weight = parse_float(form.get('weight'))
    if weight is not None:
        metrics['weight'] = weight

    thresholds = {
        'high_temp': parse_float(form.get('threshold_high_temp')),
        'low_temp': parse_float(form.get('threshold_low_temp')),
        'high_humidity': parse_float(form.get('threshold_high_humidity')),
        'high_aqi': parse_float(form.get('threshold_high_aqi'))
    }
    thresholds = compact_dict(thresholds)

    contact_prefs = {
        'channels': form.getlist('notify_channels'),
        'frequency': sanitize_input(form.get('notify_frequency'), max_length=20),
        'phone': sanitize_input(form.get('contact_phone'), max_length=30),
        'wechat': sanitize_input(form.get('contact_wechat'), max_length=50),
        'emergency_name': sanitize_input(form.get('emergency_name'), max_length=50),
        'emergency_phone': sanitize_input(form.get('emergency_phone'), max_length=30),
        'notify_family': form.get('notify_family') == 'on'
    }
    escalation_days = parse_int(form.get('escalation_days'))
    if escalation_days is not None:
        contact_prefs['escalation_days'] = escalation_days
    contact_prefs = compact_dict(contact_prefs)

    return {
        'allergies': allergies,
        'medications': medications,
        'metrics': json_or_none(metrics),
        'risk_tags': json_or_none(risk_tags),
        'weather_thresholds': json_or_none(thresholds),
        'contact_prefs': json_or_none(contact_prefs),
        'privacy_level': sanitize_input(form.get('privacy_level'), max_length=20) or 'family',
        'share_with_doctor': form.get('share_with_doctor') == 'on',
        'share_with_community': form.get('share_with_community') == 'on',
        'alert_enabled': form.get('alert_enabled') == 'on',
        'quiet_hours': sanitize_input(form.get('quiet_hours'), max_length=20)
    }


def reminder_triggered(reminder, weather):
    """判断提醒是否触发"""
    if not weather or not reminder.weather_triggers:
        return False, None
    try:
        triggers = json.loads(reminder.weather_triggers)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, None

    reasons = []
    temp = weather.temperature or 0
    humidity = weather.humidity or 0
    aqi = weather.aqi or 0

    high_temp = triggers.get('high_temp')
    low_temp = triggers.get('low_temp')
    high_humidity = triggers.get('high_humidity')
    high_aqi = triggers.get('high_aqi')

    if high_temp is not None and temp >= high_temp:
        reasons.append(f"高温≥{high_temp}°C")
    if low_temp is not None and temp <= low_temp:
        reasons.append(f"低温≤{low_temp}°C")
    if high_humidity is not None and humidity >= high_humidity:
        reasons.append(f"高湿度≥{high_humidity}%")
    if high_aqi is not None and aqi >= high_aqi:
        reasons.append(f"AQI≥{high_aqi}")

    return bool(reasons), '、'.join(reasons) if reasons else None


def member_weather_triggered(profile, weather):
    """判断成员天气触发"""
    if not weather or not profile or not profile.weather_thresholds:
        return []
    thresholds = safe_json_loads(profile.weather_thresholds, {})
    if not thresholds:
        return []
    reasons = []
    temp = getattr(weather, 'temperature', None) or 0
    humidity = getattr(weather, 'humidity', None) or 0
    aqi = getattr(weather, 'aqi', None) or 0

    high_temp = thresholds.get('high_temp')
    low_temp = thresholds.get('low_temp')
    high_humidity = thresholds.get('high_humidity')
    high_aqi = thresholds.get('high_aqi')

    if high_temp is not None and temp >= high_temp:
        reasons.append(f"高温≥{high_temp}°C")
    if low_temp is not None and temp <= low_temp:
        reasons.append(f"低温≤{low_temp}°C")
    if high_humidity is not None and humidity >= high_humidity:
        reasons.append(f"高湿度≥{high_humidity}%")
    if high_aqi is not None and aqi >= high_aqi:
        reasons.append(f"AQI≥{high_aqi}")
    return reasons


def compute_member_risk(member, profile):
    """估算成员健康风险"""
    score = 15
    reasons = []

    age = member.age or 0
    if age >= 80:
        score += 30
        reasons.append('高龄')
    elif age >= 70:
        score += 25
        reasons.append('老年')
    elif age >= 60:
        score += 20
        reasons.append('中老年')
    elif age >= 50:
        score += 10

    diseases = safe_json_loads(member.chronic_diseases, [])
    if diseases:
        score += min(30, 8 * len(diseases))
        reasons.append('慢性病')

    risk_tags = []
    metrics = {}
    thresholds = {}
    if profile:
        risk_tags = safe_json_loads(profile.risk_tags, [])
        metrics = safe_json_loads(profile.metrics, {})
        thresholds = safe_json_loads(profile.weather_thresholds, {})

    if risk_tags:
        score += min(20, 5 * len(risk_tags))
        reasons.append('风险标签')

    blood_pressure = metrics.get('blood_pressure')
    if blood_pressure and isinstance(blood_pressure, str):
        try:
            sys_val, dia_val = blood_pressure.split('/')
            sys_val = int(sys_val)
            dia_val = int(dia_val)
            if sys_val >= 140 or dia_val >= 90:
                score += 10
                reasons.append('血压偏高')
        except (ValueError, TypeError) as exc:
            logger.debug("Invalid blood pressure format: %s", exc)

    blood_sugar = metrics.get('blood_sugar')
    if blood_sugar is not None:
        try:
            if float(blood_sugar) >= 7.0:
                score += 10
                reasons.append('血糖偏高')
        except (TypeError, ValueError) as exc:
            logger.debug("Invalid blood sugar format: %s", exc)

    heart_rate = metrics.get('heart_rate')
    if heart_rate is not None:
        try:
            if int(heart_rate) >= 100:
                score += 8
                reasons.append('心率偏快')
        except (TypeError, ValueError) as exc:
            logger.debug("Invalid heart rate format: %s", exc)

    if thresholds:
        if thresholds.get('high_temp') is not None and thresholds.get('high_temp') <= 32:
            score += 6
            reasons.append('高温敏感')
        if thresholds.get('low_temp') is not None and thresholds.get('low_temp') >= 5:
            score += 6
            reasons.append('低温敏感')

    score = max(0, min(100, score))
    if score >= 70:
        level = 'high'
        label = '高风险'
    elif score >= 40:
        level = 'medium'
        label = '中风险'
    else:
        level = 'low'
        label = '低风险'

    return {
        'score': score,
        'level': level,
        'label': label,
        'reasons': list(dict.fromkeys(reasons))
    }


def compute_profile_completion(member, profile):
    """计算档案完善度"""
    fields = []
    fields.append(bool(member.relation))
    fields.append(bool(member.age))
    fields.append(bool(member.gender))
    fields.append(bool(safe_json_loads(member.chronic_diseases, [])))
    if profile:
        fields.append(bool(profile.allergies))
        fields.append(bool(profile.medications))
        metrics = safe_json_loads(profile.metrics, {})
        fields.append(bool(metrics))
        fields.append(bool(safe_json_loads(profile.risk_tags, [])))
        fields.append(bool(safe_json_loads(profile.weather_thresholds, {})))
        contact = safe_json_loads(profile.contact_prefs, {})
        fields.append(bool(contact))
    total = len(fields)
    filled = sum(1 for f in fields if f)
    percent = int(round((filled / total) * 100)) if total else 0
    return {'filled': filled, 'total': total, 'percent': percent}


def profile_to_context(profile):
    """将成员画像整理为模板可用数据"""
    if not profile:
        return {
            'allergies': '',
            'medications': '',
            'metrics': {},
            'risk_tags': [],
            'weather_thresholds': {},
            'contact_prefs': {},
            'privacy_level': 'family',
            'share_with_doctor': False,
            'share_with_community': False,
            'alert_enabled': True,
            'quiet_hours': ''
        }
    return {
        'allergies': profile.allergies or '',
        'medications': profile.medications or '',
        'metrics': safe_json_loads(profile.metrics, {}),
        'risk_tags': safe_json_loads(profile.risk_tags, []),
        'weather_thresholds': safe_json_loads(profile.weather_thresholds, {}),
        'contact_prefs': safe_json_loads(profile.contact_prefs, {}),
        'privacy_level': profile.privacy_level or 'family',
        'share_with_doctor': bool(profile.share_with_doctor),
        'share_with_community': bool(profile.share_with_community),
        'alert_enabled': True if profile.alert_enabled is None else bool(profile.alert_enabled),
        'quiet_hours': profile.quiet_hours or ''
    }
