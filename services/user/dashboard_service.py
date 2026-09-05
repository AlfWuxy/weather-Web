# -*- coding: utf-8 -*-
"""User dashboard routes."""
import json
import logging
import math
from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import current_app, render_template, request
from flask_login import current_user
from sqlalchemy import and_, or_

from core.extensions import db
from core.guest import get_guest_assessment, is_guest_user
from core.health_profiles import reminder_triggered
from core.time_utils import ensure_utc_aware, today_local, utc_to_local_date, utcnow
from core.weather import (
    canonical_weather_location,
    ensure_user_location_valid,
    get_consecutive_hot_days,
    get_openmeteo_forecast_with_cache,
    get_qweather_forecast_with_cache,
    get_weather_with_cache,
    is_air_quality_available,
    is_demo_mode,
    is_heat_action_weather_ready,
    is_live_observational_weather,
    is_qweather_production_ready,
    normalize_weather_observed_at,
    weather_source_label as get_weather_source_label,
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
from services.forecast_cards import build_forecast_cards, build_weather_only_forecast_cards
from services.forecast_service import get_forecast_service
from utils.parsers import safe_json_loads

from ._common import HEAT_RISK_LABELS, _action_plan

logger = logging.getLogger(__name__)

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


def _dashboard_alert_card(alert, now=None):
    """将提醒记录转换成首页字段，严格区分官方预警与应用提醒。"""
    now = ensure_utc_aware(now or utcnow())
    local_date = utc_to_local_date(alert.alert_date)
    level_text = (alert.alert_level or '未分级').strip()
    source = str(getattr(alert, 'source', None) or '').strip()
    starts_at = ensure_utc_aware(getattr(alert, 'starts_at', None))
    ends_at = ensure_utc_aware(getattr(alert, 'ends_at', None))
    is_verified_official = bool(
        getattr(alert, 'is_official', False)
        and source == 'QWeather'
        and starts_at is not None
        and ends_at is not None
        and starts_at <= now <= ends_at
    )
    normalized_level = level_text.lower()
    is_high = normalized_level in {'high', 'severe', 'red'} or any(
        marker in level_text for marker in ('高', '严重', '红', '橙')
    )
    return {
        'alert_type': alert.alert_type or '天气提醒',
        'alert_level': level_text,
        'alert_date_local': local_date.strftime('%Y-%m-%d') if local_date else '日期未标注',
        'location': alert.location or '地点未标注',
        'description': alert.description,
        'is_high': is_high,
        'is_official': is_verified_official,
        'kind_label': '官方预警' if is_verified_official else '应用天气提醒',
        'source_label': (
            'QWeather 官方预警'
            if is_verified_official
            else '应用阈值规则'
            if source == 'AppThreshold'
            else '来源未标明'
        ),
        'validity_label': (
            f"有效期 {utc_to_local_date(starts_at).isoformat()} 至 {utc_to_local_date(ends_at).isoformat()}"
            if is_verified_official
            else f"生成于 {local_date.isoformat()}" if local_date else '生成时间未标明'
        ),
    }


def _application_alert_level(value):
    """应用规则只使用提醒语义，禁止伪装成官方颜色预警。"""
    level = str(value or '天气提醒').strip() or '天气提醒'
    return level.replace('预警', '提醒')[:20]


def _get_or_create_application_alert(weather_service, location, weather_data, alert_locations):
    """持久化应用推导提醒；不与官方预警互相去重。"""
    alert = weather_service.generate_weather_alert(location, weather_data)
    if not alert:
        return None
    now = utcnow()
    alert_type = str(alert.get('alert_type') or '天气提醒')[:50]
    alert_level = _application_alert_level(alert.get('alert_level'))
    recent = WeatherAlert.query.filter(
        WeatherAlert.location.in_(alert_locations),
        WeatherAlert.alert_type == alert_type,
        WeatherAlert.alert_level == alert_level,
        WeatherAlert.source == 'AppThreshold',
        WeatherAlert.is_official.is_(False),
        WeatherAlert.alert_date >= now - timedelta(hours=6),
    ).order_by(WeatherAlert.alert_date.desc()).first()
    if recent:
        return recent
    record = WeatherAlert(
        alert_date=now,
        location=alert.get('location') or location,
        alert_type=alert_type,
        alert_level=alert_level,
        description=alert.get('description'),
        source='AppThreshold',
        is_official=False,
        starts_at=now,
        ends_at=None,
        affected_communities=json.dumps([location], ensure_ascii=False),
        disease_correlation=json.dumps({}, ensure_ascii=False),
    )
    db.session.add(record)
    db.session.commit()
    return record


def _dashboard_visible_alerts(alert_locations, now=None, limit=5):
    """只返回有效官方预警与最近应用提醒。"""
    now = ensure_utc_aware(now or utcnow())
    return WeatherAlert.query.filter(
        WeatherAlert.location.in_(alert_locations),
        or_(
            and_(
                WeatherAlert.is_official.is_(True),
                WeatherAlert.source == 'QWeather',
                WeatherAlert.starts_at.is_not(None),
                WeatherAlert.ends_at.is_not(None),
                WeatherAlert.starts_at <= now,
                WeatherAlert.ends_at >= now,
            ),
            and_(
                WeatherAlert.is_official.is_(False),
                WeatherAlert.alert_date >= now - timedelta(days=1),
            ),
        ),
    ).order_by(WeatherAlert.alert_date.desc()).limit(limit).all()


def _parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dashboard_weather_available(weather_data):
    """兼容旧调用：weather_available 只表示真实实况可展示。"""
    return is_live_observational_weather(weather_data)


def _forecast_weather_context(weather_data):
    """提取真实和风实况中的有限空气质量值，供未来日代理链使用。"""
    if not (
        is_qweather_production_ready(weather_data)
        and is_air_quality_available(weather_data)
    ):
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
    """首页优先展示和风健康预测，失败时仅展示 Open-Meteo 天气。"""
    qweather_days, _, meta = get_qweather_forecast_with_cache(location, days=7)
    if len(qweather_days or []) < 7:
        logger.warning(
            "首页和风7日预报不可用: location=%s meta=%s count=%s",
            location,
            meta,
            len(qweather_days or []),
        )
        openmeteo_days, _, openmeteo_meta = get_openmeteo_forecast_with_cache(location, days=7)
        if len(openmeteo_days or []) < 7:
            logger.warning(
                "首页Open-Meteo 7日预报不可用: location=%s meta=%s count=%s",
                location,
                openmeteo_meta,
                len(openmeteo_days or []),
            )
            return []
        return build_weather_only_forecast_cards(openmeteo_days, start_date)

    health_forecasts = []
    if not (
        is_qweather_production_ready(current_weather)
        and is_air_quality_available(current_weather)
    ):
        return build_forecast_cards(qweather_days, [], start_date)
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
    weather_location = canonical_weather_location(user_location)
    alert_locations = [user_location]
    if user_location in ('都昌', '都昌县'):
        alert_locations = ['都昌', '都昌县']
    weather_source_city = resolve_weather_city_label(user_location)
    weather_data, used_cache = get_weather_with_cache(user_location)
    weather_is_mock = bool(weather_data.get('is_mock'))
    display_weather_available = _dashboard_weather_available(weather_data)
    heat_action_weather_ready = is_heat_action_weather_ready(weather_data)
    air_quality_available = is_air_quality_available(weather_data)
    qweather_production_ready = is_qweather_production_ready(weather_data)
    weather_source_name = get_weather_source_label(weather_data)
    # 兼容旧模板：weather_available 仅代表可以展示实况，不代表可进入健康风险链。
    weather_available = display_weather_available

    from services.weather_service import WeatherService
    weather_service = WeatherService()
    if not qweather_production_ready:
        extreme_result = {'is_extreme': False, 'conditions': []}
    else:
        try:
            extreme_result = weather_service.identify_extreme_weather(weather_data)
        except Exception as exc:
            logger.warning("极端天气识别失败，已跳过: %s", exc)
            extreme_result = {'is_extreme': False, 'conditions': []}

    persisted_weather = WeatherData.query.filter_by(
        date=today,
        location=weather_location,
    ).order_by(WeatherData.id.desc()).first()

    if qweather_production_ready:
        if not persisted_weather:
            persisted_weather = WeatherData(date=today, location=weather_location)
            db.session.add(persisted_weather)
        persisted_weather.temperature = weather_data.get('temperature')
        persisted_weather.temperature_max = weather_data.get('temperature_max')
        persisted_weather.temperature_min = weather_data.get('temperature_min')
        persisted_weather.humidity = weather_data.get('humidity')
        persisted_weather.pressure = weather_data.get('pressure')
        persisted_weather.weather_condition = weather_data.get('weather_condition')
        persisted_weather.wind_speed = weather_data.get('wind_speed')
        persisted_weather.pm25 = weather_data.get('pm25')
        persisted_weather.aqi = weather_data.get('aqi')
        persisted_weather.data_source = 'QWeather'
        observed_at = normalize_weather_observed_at(weather_data.get('observed_at'))
        persisted_weather.observed_at = datetime.fromisoformat(observed_at)
        air_observed_at = normalize_weather_observed_at(weather_data.get('air_observed_at'))
        persisted_weather.air_observed_at = (
            datetime.fromisoformat(air_observed_at)
            if air_observed_at is not None
            else None
        )
        persisted_weather.quality_version = 1
        persisted_weather.air_quality_available = air_quality_available
        persisted_weather.is_extreme = extreme_result['is_extreme']
        persisted_weather.extreme_type = '、'.join([c['type'] for c in extreme_result['conditions']]) if extreme_result['is_extreme'] else None
        db.session.commit()

    if display_weather_available:
        weather = SimpleNamespace(**weather_data)
        weather.is_extreme = bool(extreme_result.get('is_extreme')) if qweather_production_ready else False
        weather.extreme_type = (
            '、'.join([c['type'] for c in extreme_result.get('conditions', [])])
            if weather.is_extreme
            else None
        )
    else:
        weather = None

    heat_service = HeatActionService()
    if not heat_action_weather_ready:
        heat_result = None
        heat_risk_label = '暂不可用'
        heat_actions = []
    else:
        consecutive_hot_days = get_consecutive_hot_days(
            weather_location,
            today_max=weather_data.get('temperature_max'),
            weather_data=weather_data,
        )
        heat_result = heat_service.calculate_heat_risk(
            weather_data,
            consecutive_hot_days=consecutive_hot_days
        )
        heat_risk_label = HEAT_RISK_LABELS.get(heat_result['risk_level'], '低风险')
        heat_actions = _action_plan(heat_risk_label)
    dashboard_hero_theme = _dashboard_hero_theme(
        getattr(weather, 'temperature', None) if display_weather_available else None
    )
    dashboard_metric_cards = [] if is_guest else _dashboard_metric_cards(current_user.id)
    family_members = []
    if not is_guest and getattr(current_user, 'role', None) != 'community':
        family_members = FamilyMember.query.filter_by(user_id=current_user.id).order_by(
            FamilyMember.created_at.desc()
        ).limit(3).all()
    forecast_days = _dashboard_forecast_days(user_location, today, weather_data)

    # 应用算法只能生成行动提醒，和官方 QWeather 预警分开保存。
    if qweather_production_ready and extreme_result['is_extreme'] and not used_cache:
        _get_or_create_application_alert(
            weather_service,
            user_location,
            weather_data,
            alert_locations,
        )

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

    # 官方预警必须处于有效期；应用提醒只保留最近 24 小时。
    alert_now = utcnow()
    alerts = _dashboard_visible_alerts(alert_locations, now=alert_now)

    # 缓存命中时若没有近期提醒，仍可基于当前可信和风数据补一条应用提醒。
    if qweather_production_ready and not alerts and weather and weather.is_extreme:
        application_alert = _get_or_create_application_alert(
            weather_service,
            user_location,
            weather_data,
            alert_locations,
        )
        if application_alert:
            alerts = [application_alert]

    # 用药提醒（根据天气触发）
    reminders = []
    if not is_guest and qweather_production_ready and weather:
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
            display_weather_available=display_weather_available,
            heat_action_weather_ready=heat_action_weather_ready,
            air_quality_available=air_quality_available,
            qweather_production_ready=qweather_production_ready,
            weather_source_label=weather_source_name,
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

    alert_cards = [_dashboard_alert_card(alert, now=alert_now) for alert in alerts]

    return render_template('user_dashboard.html',
                         weather=weather if weather_available else None,
                         weather_source_city=weather_source_city,
                         weather_is_mock=weather_is_mock,
                         weather_available=weather_available,
                         display_weather_available=display_weather_available,
                         heat_action_weather_ready=heat_action_weather_ready,
                         air_quality_available=air_quality_available,
                         qweather_production_ready=qweather_production_ready,
                         weather_source_label=weather_source_name,
                         demo_mode=demo_mode,
                         assessment=latest_assessment,
                         assessment_explain=assessment_explain,
                         heat_result=heat_result,
                         heat_risk_label=heat_risk_label,
                         heat_actions=heat_actions,
                         dashboard_hero_theme=dashboard_hero_theme,
                         dashboard_metric_cards=dashboard_metric_cards,
                         family_members=family_members,
                         forecast_days=forecast_days,
                         alerts=alert_cards,
                         reminders=reminders,
                         notifications=notifications,
                         is_guest=is_guest)


def elder_dashboard():
    """极简老人模式入口"""
    return user_dashboard(force_elder=True)
