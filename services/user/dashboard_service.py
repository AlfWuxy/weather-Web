# -*- coding: utf-8 -*-
"""User dashboard routes."""
import json
import logging
import math
from datetime import timedelta
from types import SimpleNamespace

from flask import current_app, render_template, request
from flask_login import current_user

from core.extensions import db
from core.guest import get_guest_assessment, is_guest_user
from core.health_profiles import reminder_triggered
from core.time_utils import today_local, utcnow
from core.weather import (
    ensure_user_location_valid,
    get_consecutive_hot_days,
    get_qweather_forecast_with_cache,
    get_weather_with_cache,
    is_demo_mode,
    is_qweather_online_weather,
    resolve_weather_city_label
)
from core.db_models import (
    FamilyMember,
    FamilyMemberProfile,
    HealthRiskAssessment,
    MedicationReminder,
    Notification,
    WeatherAlert,
    WeatherData
)
from services.heat_action_service import HeatActionService
from services.forecast_cards import build_forecast_cards
from services.forecast_service import get_forecast_service
from utils.parsers import safe_json_loads

from ._common import HEAT_RISK_LABELS, _action_plan

logger = logging.getLogger(__name__)

_REQUIRED_DASHBOARD_WEATHER_FIELDS = (
    'temperature',
    'temperature_max',
    'temperature_min',
    'humidity',
)


def _clamp(value, lower=0.0, upper=1.0):
    return max(lower, min(upper, value))


def _lerp(start, end, amount):
    return start + (end - start) * amount


def _dashboard_hero_theme(temperature):
    """按当天温度线性生成首页首屏橙色主题。"""
    try:
        temp = float(temperature)
    except (TypeError, ValueError):
        temp = None

    effective_temp = temp if temp is not None else 22.0
    intensity = _clamp((effective_temp - 8.0) / 27.0)

    hue = round(_lerp(34, 22, intensity))
    primary_sat = round(_lerp(56, 82, intensity))
    primary_light = round(_lerp(84, 61, intensity))
    secondary_sat = round(_lerp(62, 78, intensity))
    secondary_light = round(_lerp(91, 70, intensity))
    soft_sat = round(_lerp(72, 82, intensity))
    soft_light = round(_lerp(97, 82, intensity))

    hot_hero = intensity >= 0.62
    readable_text = '#FFFFFF' if hot_hero else 'var(--yl-ink)'
    readable_muted = 'rgba(255, 255, 255, .84)' if hot_hero else 'var(--yl-ink-soft)'
    panel_bg = 'rgba(255, 255, 255, .20)' if hot_hero else 'rgba(255, 255, 255, .62)'
    panel_border = 'rgba(255, 255, 255, .30)' if hot_hero else 'rgba(255, 255, 255, .72)'
    label_bg = 'rgba(255, 255, 255, .18)' if hot_hero else 'rgba(255, 255, 255, .66)'
    score_color = '#FFFFFF' if hot_hero else 'var(--yl-risk-mid)'

    css_vars = {
        'primary': f'hsl({hue}, {primary_sat}%, {primary_light}%)',
        'secondary': f'hsl({hue + 6}, {secondary_sat}%, {secondary_light}%)',
        'soft': f'hsl({hue + 11}, {soft_sat}%, {soft_light}%)',
        'ring': 'rgba(255, 255, 255, .20)' if hot_hero else 'rgba(238, 126, 45, .18)',
        'text': readable_text,
        'muted': readable_muted,
        'label-color': readable_text if hot_hero else 'var(--yl-orange-600)',
        'chip-bg': label_bg,
        'panel-bg': panel_bg,
        'panel-border': panel_border,
        'score': score_color,
        'score-low': score_color if hot_hero else 'var(--yl-success)',
        'score-mid': score_color if hot_hero else 'var(--yl-risk-mid)',
        'score-high': score_color if hot_hero else 'var(--yl-risk-high)',
        'shadow-alpha': f'{_lerp(0.05, 0.14, intensity):.3f}',
    }
    style = '; '.join(f'--yl-hero-{name}: {value}' for name, value in css_vars.items()) + ';'
    return {
        'temperature': temp,
        'effective_temperature': effective_temp,
        'intensity': round(intensity, 3),
        'style': style,
    }


def _parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dashboard_weather_available(weather_data):
    """真实和风天气且热风险关键输入完整时才允许展示和落库。"""
    if not is_qweather_online_weather(weather_data):
        return False
    for field in _REQUIRED_DASHBOARD_WEATHER_FIELDS:
        value = _parse_float(weather_data.get(field))
        if value is None or not math.isfinite(value):
            return False
    return True


def _forecast_weather_context(weather_data):
    """提取真实和风实况中的有限空气质量值，供未来日代理链使用。"""
    if not is_qweather_online_weather(weather_data):
        return {}
    context = {}
    for field in ('pm25', 'aqi'):
        value = _parse_float((weather_data or {}).get(field))
        if value is not None and math.isfinite(value):
            context[field] = value
    return context


def _parse_systolic(value):
    """从画像指标里提取收缩压，支持 138/82 或单个数字。"""
    if isinstance(value, str) and '/' in value:
        value = value.split('/', 1)[0]
    return _parse_float(value)


def _flat_metric_series(value, length=30):
    """没有历史序列时，仅用当前登记值形成定位线，避免伪造趋势。"""
    numeric = _parse_float(value)
    if numeric is None:
        return '[]'
    return json.dumps([round(numeric, 1)] * length)


def _dashboard_metric_cards(user_id):
    """构造首页健康指标动效卡，只使用家庭成员画像中的已登记数值。"""
    members = FamilyMember.query.filter_by(user_id=user_id).order_by(
        FamilyMember.created_at.desc()
    ).all()
    if not members:
        return []

    profiles = FamilyMemberProfile.query.filter(
        FamilyMemberProfile.member_id.in_([member.id for member in members])
    ).all()
    profile_map = {profile.member_id: profile for profile in profiles}
    cards = {}

    def add_card(key, member, value, display_value, band_min, band_max, label, unit, icon, color):
        if key in cards or value is None:
            return
        anomaly_idx = [29] if value < band_min or value > band_max else []
        cards[key] = {
            'label': label,
            'unit': unit,
            'icon': icon,
            'color': color,
            'member_name': member.name,
            'current_display': display_value,
            'values_json': _flat_metric_series(value),
            'band_min': band_min,
            'band_max': band_max,
            'anomalies_json': json.dumps(anomaly_idx),
        }

    for member in members:
        profile = profile_map.get(member.id)
        metrics = safe_json_loads(profile.metrics, {}) if profile and profile.metrics else {}
        if not isinstance(metrics, dict):
            continue

        sbp = _parse_systolic(metrics.get('blood_pressure'))
        if sbp is not None:
            raw_bp = metrics.get('blood_pressure')
            display = f"{raw_bp} mmHg" if raw_bp else f"{sbp:g} mmHg"
            add_card('sbp', member, sbp, display, 90, 135, '收缩压', 'mmHg', 'heart-pulse', '#C7472E')

        heart_rate = _parse_float(metrics.get('heart_rate'))
        if heart_rate is not None:
            add_card('heart_rate', member, heart_rate, f"{heart_rate:g} bpm", 60, 100, '心率', 'bpm', 'activity', '#E8A23C')

        blood_sugar = _parse_float(metrics.get('blood_sugar'))
        if blood_sugar is not None:
            add_card('blood_sugar', member, blood_sugar, f"{blood_sugar:g} mmol/L", 3.9, 6.1, '空腹血糖', 'mmol/L', 'droplet-half', '#4A89C4')

        if len(cards) == 3:
            break

    return [cards[key] for key in ('sbp', 'heart_rate', 'blood_sugar') if key in cards]


def _dashboard_forecast_days(location, start_date, current_weather=None):
    """首页 7 日预测只使用和风实时预报，失败时不展示演示风险。"""
    qweather_days, _, meta = get_qweather_forecast_with_cache(location, days=7)
    if len(qweather_days or []) < 7:
        logger.warning(
            "首页和风7日预报不可用: location=%s meta=%s count=%s",
            location,
            meta,
            len(qweather_days or []),
        )
        return []

    health_forecasts = []
    try:
        health_forecasts, _ = get_forecast_service().generate_7day_forecast(
            qweather_days,
            start_date=start_date,
            context=_forecast_weather_context(current_weather),
        )
    except Exception as exc:
        logger.warning("首页7日健康预测生成失败，仅展示和风天气: %s", exc)
    return build_forecast_cards(qweather_days, health_forecasts, start_date)


def user_dashboard(force_elder=False):
    """用户仪表板"""
    elder_mode = force_elder or (
        request.args.get('mode') == 'elder'
        and current_app.config.get('FEATURE_ELDER_MODE')
    )
    is_guest = is_guest_user(current_user)
    demo_mode = is_demo_mode()
    # 获取当前天气
    today = today_local()
    user_location = ensure_user_location_valid()
    alert_locations = [user_location]
    if user_location in ('都昌', '都昌县'):
        alert_locations = ['都昌', '都昌县']
    weather_source_city = resolve_weather_city_label(user_location)
    weather_data, used_cache = get_weather_with_cache(user_location)
    weather_is_mock = bool(weather_data.get('is_mock'))
    weather_available = _dashboard_weather_available(weather_data)

    from services.weather_service import WeatherService
    weather_service = WeatherService()
    if not weather_available:
        extreme_result = {'is_extreme': False, 'conditions': []}
    else:
        try:
            extreme_result = weather_service.identify_extreme_weather(weather_data)
        except Exception as exc:
            logger.warning("极端天气识别失败，已跳过: %s", exc)
            extreme_result = {'is_extreme': False, 'conditions': []}

    weather = WeatherData.query.filter_by(
        date=today,
        location=user_location
    ).order_by(WeatherData.id.desc()).first()

    if weather_available and (not weather or not used_cache):
        if not weather:
            weather = WeatherData(date=today, location=user_location)
            db.session.add(weather)
        weather.temperature = weather_data.get('temperature')
        weather.temperature_max = weather_data.get('temperature_max')
        weather.temperature_min = weather_data.get('temperature_min')
        weather.humidity = weather_data.get('humidity')
        weather.pressure = weather_data.get('pressure')
        weather.weather_condition = weather_data.get('weather_condition')
        weather.wind_speed = weather_data.get('wind_speed')
        weather.pm25 = weather_data.get('pm25')
        weather.aqi = weather_data.get('aqi')
        weather.is_extreme = extreme_result['is_extreme']
        weather.extreme_type = '、'.join([c['type'] for c in extreme_result['conditions']]) if extreme_result['is_extreme'] else None
        db.session.commit()

    if weather_available and not weather:
        weather = SimpleNamespace(**weather_data)
        weather.is_extreme = extreme_result['is_extreme']
        weather.extreme_type = '、'.join([c['type'] for c in extreme_result['conditions']]) if extreme_result['is_extreme'] else None

    heat_service = HeatActionService()
    if not weather_available:
        heat_result = None
        heat_risk_label = '暂不可用'
        heat_actions = []
    else:
        consecutive_hot_days = get_consecutive_hot_days(
            user_location,
            today_max=weather_data.get('temperature_max')
        )
        heat_result = heat_service.calculate_heat_risk(
            weather_data,
            consecutive_hot_days=consecutive_hot_days
        )
        heat_risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
        heat_actions = _action_plan(heat_risk_label)
    dashboard_hero_theme = _dashboard_hero_theme(
        getattr(weather, 'temperature', None) if weather_available else None
    )
    dashboard_metric_cards = [] if is_guest else _dashboard_metric_cards(current_user.id)
    forecast_days = _dashboard_forecast_days(user_location, today, weather_data)

    # 如果是极端天气，生成预警（避免重复）
    if weather_available and extreme_result['is_extreme'] and not used_cache:
        recent_alert = WeatherAlert.query.filter(
            WeatherAlert.location.in_(alert_locations),
            WeatherAlert.alert_date >= utcnow() - timedelta(hours=6)
        ).first()
        if not recent_alert:
            alert = weather_service.generate_weather_alert(user_location, weather_data)
            if alert:
                weather_alert = WeatherAlert(
                    alert_date=utcnow(),
                    location=alert['location'],
                    alert_type=alert['alert_type'],
                    alert_level=alert['alert_level'],
                    description=alert['description'],
                    affected_communities=json.dumps([user_location]),
                    disease_correlation=json.dumps({})
                )
                db.session.add(weather_alert)
                db.session.commit()

    # 获取最新风险评估
    if is_guest:
        latest_assessment = get_guest_assessment()
    else:
        latest_assessment = HealthRiskAssessment.query.filter_by(
            user_id=current_user.id
        ).order_by(HealthRiskAssessment.assessment_date.desc()).first()

    assessment_explain = {}
    if latest_assessment and getattr(latest_assessment, 'explain', None):
        assessment_explain = safe_json_loads(latest_assessment.explain, {})

    # 获取天气预警（最近24小时）
    alerts = WeatherAlert.query.filter(
        WeatherAlert.alert_date >= utcnow() - timedelta(days=1),
        WeatherAlert.location.in_(alert_locations)
    ).order_by(WeatherAlert.alert_date.desc()).limit(5).all()

    # 如果没有预警但有极端天气，创建预警
    if weather_available and not alerts and weather and weather.is_extreme:
        from services.weather_service import WeatherService
        weather_service = WeatherService()
        weather_data = {
            'temperature': weather.temperature,
            'temperature_max': weather.temperature_max,
            'temperature_min': weather.temperature_min,
            'humidity': weather.humidity,
            'aqi': weather.aqi,
            'wind_speed': weather.wind_speed,
        }
        recent_alert = WeatherAlert.query.filter(
            WeatherAlert.location.in_(alert_locations),
            WeatherAlert.alert_date >= utcnow() - timedelta(hours=6)
        ).first()
        if not recent_alert:
            alert = weather_service.generate_weather_alert(user_location, weather_data)
            if alert:
                weather_alert = WeatherAlert(
                    alert_date=utcnow(),
                    location=alert['location'],
                    alert_type=alert['alert_type'],
                    alert_level=alert['alert_level'],
                    description=alert['description'],
                    affected_communities=json.dumps([user_location]),
                    disease_correlation=json.dumps({})
                )
                db.session.add(weather_alert)
                db.session.commit()
                alerts = [weather_alert]

    # 用药提醒（根据天气触发）
    reminders = []
    if not is_guest and weather_available and weather:
        now = utcnow()
        reminders_query = MedicationReminder.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).all()
        updated = False
        for reminder in reminders_query:
            if reminder.member_id:
                member = FamilyMember.query.filter_by(id=reminder.member_id, user_id=current_user.id).first()
                if not member or not member.chronic_diseases:
                    continue
            else:
                if not current_user.has_chronic_disease:
                    continue
            triggered, reason = reminder_triggered(reminder, weather)
            if triggered:
                last_notified = reminder.last_notified_at
                if not last_notified or last_notified.date() != now.date():
                    reminder.last_notified_at = now
                    updated = True
                reminders.append({
                    'medicine_name': reminder.medicine_name,
                    'dosage': reminder.dosage,
                    'time_of_day': reminder.time_of_day,
                    'reason': reason
                })
        if updated:
            db.session.commit()

    notifications = []
    if current_app.config.get('FEATURE_NOTIFICATIONS') and not is_guest:
        notifications = Notification.query.filter_by(user_id=current_user.id).order_by(
            Notification.created_at.desc()
        ).limit(5).all()

    if elder_mode:
        elder_actions = []
        explain_block = assessment_explain.get('explain') if isinstance(assessment_explain, dict) else None
        if explain_block and explain_block.get('actions'):
            elder_actions = explain_block.get('actions', [])
        elif latest_assessment and latest_assessment.recommendations:
            recs = safe_json_loads(latest_assessment.recommendations, [])
            elder_actions = [r.get('advice') for r in recs if r.get('advice')]
        elder_actions = elder_actions[:3]

        emergency_contact = None
        if not is_guest:
            profiles = FamilyMemberProfile.query.join(FamilyMember).filter(
                FamilyMember.user_id == current_user.id
            ).all()
            for profile in profiles:
                contact = safe_json_loads(profile.contact_prefs, {})
                if contact.get('emergency_phone'):
                    emergency_contact = {
                        'name': contact.get('emergency_name') or '紧急联系人',
                        'phone': contact.get('emergency_phone')
                    }
                    break

        return render_template(
            'elder_dashboard.html',
            weather=weather if weather_available else None,
            weather_source_city=weather_source_city,
            weather_is_mock=weather_is_mock,
            weather_available=weather_available,
            demo_mode=demo_mode,
            assessment=latest_assessment,
            assessment_explain=assessment_explain,
            elder_actions=elder_actions,
            emergency_contact=emergency_contact,
            heat_result=heat_result,
            heat_risk_label=heat_risk_label,
            heat_actions=heat_actions,
            is_guest=is_guest
        )

    return render_template('user_dashboard.html',
                         weather=weather if weather_available else None,
                         weather_source_city=weather_source_city,
                         weather_is_mock=weather_is_mock,
                         weather_available=weather_available,
                         demo_mode=demo_mode,
                         assessment=latest_assessment,
                         assessment_explain=assessment_explain,
                         heat_result=heat_result,
                         heat_risk_label=heat_risk_label,
                         heat_actions=heat_actions,
                         dashboard_hero_theme=dashboard_hero_theme,
                         dashboard_metric_cards=dashboard_metric_cards,
                         forecast_days=forecast_days,
                         alerts=alerts,
                         reminders=reminders,
                         notifications=notifications,
                         is_guest=is_guest)


def elder_dashboard():
    """极简老人模式入口"""
    return user_dashboard(force_elder=True)
