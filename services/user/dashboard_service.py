# -*- coding: utf-8 -*-
"""User dashboard routes."""
import json
import logging
import math
from types import SimpleNamespace

from flask import current_app, has_app_context, render_template, request
from flask_login import current_user

from core.extensions import db
from core.guest import get_guest_assessment, is_guest_user
from core.health_profiles import reminder_triggered
from core.time_utils import today_local, utc_to_local_date, utcnow
from core.weather import (
    ensure_user_location_valid,
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
)
from services.forecast_cards import build_forecast_cards
from utils.parsers import safe_json_loads

logger = logging.getLogger(__name__)

_REQUIRED_DASHBOARD_WEATHER_FIELDS = (
    'temperature',
    'temperature_max',
    'temperature_min',
    'humidity',
)


def get_bootstrap_payload(*args, **kwargs):
    """延迟导入快照服务，避免 services.user 包初始化时形成循环依赖。"""
    from services.miniprogram_service import get_bootstrap_payload as load_payload

    return load_payload(*args, **kwargs)


def snapshot_display_time(value):
    """延迟导入快照时间格式化函数。"""
    from services.miniprogram_service import snapshot_display_time as format_time

    return format_time(value)


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


def _dashboard_alert_card(alert):
    """将 WeatherAlert 真字段转换成首页展示字段。"""
    local_date = utc_to_local_date(alert.alert_date)
    level_text = (alert.alert_level or '未分级').strip()
    normalized_level = level_text.lower()
    is_high = normalized_level in {'high', 'extreme', 'severe', 'moderate', 'red'} or any(
        marker in level_text for marker in ('高', '严重', '红', '橙')
    )
    location = alert.location or '地点未标注'
    if has_app_context():
        canonical = str(
            current_app.config.get('QWEATHER_CANONICAL_LOCATION')
            or current_app.config.get('DEFAULT_LOCATION')
            or ''
        ).strip()
        if canonical and location == canonical:
            location = str(current_app.config.get('DEFAULT_CITY') or '都昌县').strip()
            if location == '都昌':
                location = '都昌县'
    return {
        'alert_type': alert.alert_type or '天气预警',
        'alert_level': level_text,
        'alert_date_local': local_date.strftime('%Y-%m-%d') if local_date else '日期未标注',
        'location': location,
        'description': alert.description,
        'is_high': is_high,
    }


def _dashboard_snapshot_alert_card(warning, location):
    """将同一县级快照中的官方预警转换成首页卡片。"""
    row = warning if isinstance(warning, dict) else {}
    level_text = str(row.get('level') or row.get('severity') or '未分级').strip()
    normalized_level = level_text.lower()
    is_high = normalized_level in {'high', 'extreme', 'severe', 'moderate', 'red'} or any(
        marker in level_text for marker in ('高', '严重', '红', '橙')
    )
    published_at = str(
        row.get('start_time')
        or row.get('issued_at')
        or row.get('published_at')
        or ''
    ).strip()
    date_text = published_at[:10] if len(published_at) >= 10 else '日期未标注'
    return {
        'alert_type': str(row.get('title') or row.get('type') or '天气预警').strip(),
        'alert_level': level_text,
        'alert_date_local': date_text,
        'location': location or '都昌县',
        'description': str(row.get('text') or row.get('instruction') or '').strip(),
        'is_high': is_high,
    }


def _dashboard_alert_locations(user_location):
    """都昌页面同时读取县名和唯一 canonical 坐标写入的预警。"""
    locations = [str(user_location or '').strip()]
    canonical = str(
        current_app.config.get('QWEATHER_CANONICAL_LOCATION')
        or current_app.config.get('DEFAULT_LOCATION')
        or ''
    ).strip()
    if canonical:
        # 产品只提供都昌县天气，县内村庄同样复用县级官方预警。
        locations.extend(['都昌', '都昌县', canonical])
    return list(dict.fromkeys(location for location in locations if location))


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


def _dashboard_snapshot_forecast_days(snapshot, start_date):
    """把已落库的逐日天气风险直接转换成首页卡片。"""
    if snapshot.get('forecast_stale'):
        return []
    forecast = snapshot.get('forecast')
    if not isinstance(forecast, list):
        return []
    cards = build_forecast_cards(forecast, [], start_date)
    source_by_date = {
        str(item.get('date') or item.get('forecast_date')): item
        for item in forecast
        if isinstance(item, dict) and (item.get('date') or item.get('forecast_date'))
    }
    for card in cards:
        item = source_by_date.get(card.get('full_date')) or {}
        score = _parse_float(item.get('risk_score'))
        available = (
            item.get('risk_available') is True
            and score is not None
            and math.isfinite(score)
        )
        if not available:
            continue
        label = str(item.get('risk_level') or '待计算')
        card.update({
            'risk_available': True,
            'risk_score': max(0, min(100, int(round(score)))),
            'risk_label': label,
            'risk_level': (
                'high' if '高' in label or '极' in label
                else 'mid' if '中' in label
                else 'low'
            ),
        })
    return cards


def user_dashboard(force_elder=False):
    """用户仪表板"""
    elder_mode = force_elder or (
        request.args.get('mode') == 'elder'
        and current_app.config.get('FEATURE_ELDER_MODE')
    )
    is_guest = is_guest_user(current_user)
    demo_mode = is_demo_mode()
    # 首页和公开风险、小程序共用同一份只读快照；缺少快照时统一安全降级。
    today = today_local()
    user_location = ensure_user_location_valid()
    snapshot = get_bootstrap_payload()
    snapshot_id = snapshot.get('snapshot_id')
    snapshot_mode = bool(snapshot_id)
    snapshot_location = snapshot.get('location') or {}
    weather_source_city = str(
        snapshot_location.get('name')
        or resolve_weather_city_label(user_location)
    )
    weather_data = snapshot.get('current') or {}
    if not snapshot_mode or snapshot.get('current_stale'):
        weather_data = {}
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

    weather = None
    if weather_available:
        weather = SimpleNamespace(**weather_data)
        weather.is_extreme = extreme_result['is_extreme']
        weather.extreme_type = '、'.join([c['type'] for c in extreme_result['conditions']]) if extreme_result['is_extreme'] else None

    risk = snapshot.get('risk') or {} if snapshot_mode else {}
    calculation = risk.get('calculation') if isinstance(risk, dict) else {}
    stored_heat_result = calculation.get('heat_result') if isinstance(calculation, dict) else None
    snapshot_risk_ready = (
        snapshot_mode
        and not snapshot.get('risk_stale')
        and risk.get('available') is True
        and isinstance(stored_heat_result, dict)
    )
    if snapshot_risk_ready:
        heat_result = dict(stored_heat_result)
        heat_risk_label = risk.get('level') or '暂不可用'
        heat_actions = snapshot.get('actions') or []
    else:
        heat_result = None
        heat_risk_label = '暂不可用'
        heat_actions = []
    dashboard_hero_theme = _dashboard_hero_theme(
        getattr(weather, 'temperature', None) if weather_available else None
    )
    dashboard_metric_cards = [] if is_guest else _dashboard_metric_cards(current_user.id)
    forecast_days = _dashboard_snapshot_forecast_days(snapshot, today) if snapshot_mode else []

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

    # 预警与温度、风险共用同一快照；来源未知、不可用或过期时不展示旧预警。
    warning_state = (snapshot.get('source_status') or {}).get('warnings') or {}
    warnings_ready = (
        snapshot_mode
        and not snapshot.get('warnings_stale')
        and warning_state.get('available') is True
    )
    alerts = [
        _dashboard_snapshot_alert_card(item, weather_source_city)
        for item in (snapshot.get('warnings') or [])[:5]
    ] if warnings_ready else []

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
            is_guest=is_guest,
            weather_snapshot_id=snapshot_id,
            weather_snapshot_fetched_at=snapshot.get('fetched_at'),
            weather_snapshot_display_time=snapshot_display_time(snapshot.get('fetched_at')),
            weather_source_status=snapshot.get('source_status') or {},
        )

    alert_cards = alerts

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
                         alerts=alert_cards,
                         reminders=reminders,
                         notifications=notifications,
                         is_guest=is_guest,
                         weather_snapshot_id=snapshot_id,
                         weather_snapshot_fetched_at=snapshot.get('fetched_at'),
                         weather_snapshot_display_time=snapshot_display_time(snapshot.get('fetched_at')),
                         weather_source_status=snapshot.get('source_status') or {})


def elder_dashboard():
    """极简老人模式入口"""
    return user_dashboard(force_elder=True)
