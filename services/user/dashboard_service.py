# -*- coding: utf-8 -*-
"""User dashboard routes."""
import json
import logging
from datetime import timedelta
from types import SimpleNamespace

from flask import current_app, redirect, render_template, request, url_for
from flask_login import current_user

from core.extensions import db
from core.guest import get_guest_assessment, is_guest_user
from core.health_profiles import reminder_triggered
from core.time_utils import today_local, utcnow
from core.weather import (
    ensure_user_location_valid,
    get_consecutive_hot_days,
    get_weather_with_cache,
    is_demo_mode,
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
from utils.parsers import safe_json_loads

from ._common import HEAT_RISK_LABELS, _action_plan

logger = logging.getLogger(__name__)


def user_dashboard():
    """用户仪表板"""
    elder_mode = request.args.get('mode') == 'elder' and current_app.config.get('FEATURE_ELDER_MODE')
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

    from services.weather_service import WeatherService
    weather_service = WeatherService()
    try:
        extreme_result = weather_service.identify_extreme_weather(weather_data)
    except Exception as exc:
        logger.warning("极端天气识别失败，已跳过: %s", exc)
        extreme_result = {'is_extreme': False, 'conditions': []}

    weather = WeatherData.query.filter_by(
        date=today,
        location=user_location
    ).order_by(WeatherData.id.desc()).first()

    if not weather or not used_cache:
        if not weather:
            weather = WeatherData(date=today, location=user_location)
            db.session.add(weather)
        weather.temperature = weather_data.get('temperature', 20)
        weather.temperature_max = weather_data.get('temperature_max', 25)
        weather.temperature_min = weather_data.get('temperature_min', 15)
        weather.humidity = weather_data.get('humidity', 60)
        weather.pressure = weather_data.get('pressure', 1013)
        weather.weather_condition = weather_data.get('weather_condition', '未知')
        weather.wind_speed = weather_data.get('wind_speed', 2.0)
        weather.pm25 = weather_data.get('pm25', 35)
        weather.aqi = weather_data.get('aqi', 50)
        weather.is_extreme = extreme_result['is_extreme']
        weather.extreme_type = '、'.join([c['type'] for c in extreme_result['conditions']]) if extreme_result['is_extreme'] else None
        db.session.commit()

    if not weather:
        weather = SimpleNamespace(**weather_data)
        weather.is_extreme = extreme_result['is_extreme']
        weather.extreme_type = '、'.join([c['type'] for c in extreme_result['conditions']]) if extreme_result['is_extreme'] else None

    heat_service = HeatActionService()
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

    # 如果是极端天气，生成预警（避免重复）
    if extreme_result['is_extreme'] and not used_cache:
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
    if not alerts and weather and weather.is_extreme:
        from services.weather_service import WeatherService
        weather_service = WeatherService()
        weather_data = {
            'temperature': weather.temperature or 20,
            'temperature_max': weather.temperature_max or 25,
            'temperature_min': weather.temperature_min or 15,
            'humidity': weather.humidity or 60,
            'aqi': weather.aqi or 50,
            'wind_speed': weather.wind_speed or 2.0
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
    if not is_guest and weather:
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
            weather=weather,
            weather_source_city=weather_source_city,
            weather_is_mock=weather_is_mock,
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
                         weather=weather,
                         weather_source_city=weather_source_city,
                         weather_is_mock=weather_is_mock,
                         demo_mode=demo_mode,
                         assessment=latest_assessment,
                         assessment_explain=assessment_explain,
                         heat_result=heat_result,
                         heat_risk_label=heat_risk_label,
                         heat_actions=heat_actions,
                         alerts=alerts,
                         reminders=reminders,
                         notifications=notifications,
                         is_guest=is_guest)


def elder_dashboard():
    """极简老人模式入口"""
    return redirect(url_for('user.user_dashboard', mode='elder'))
