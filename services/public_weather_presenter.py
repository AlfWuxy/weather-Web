# -*- coding: utf-8 -*-
"""公开县级信息的呈现边界，不读取家庭健康档案。"""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from core.db_models import WeatherAlert
from core.extensions import db
from core.time_utils import utcnow
from core.weather import resolve_weather_city_label


def public_official_alerts(location):
    """只展示来源及有效期明确的官方预警；查询失败不能解释为无预警。"""
    now = utcnow()
    locations = {location, resolve_weather_city_label(location)}
    if locations & {'都昌', '都昌县'}:
        locations.update({'都昌', '都昌县'})
    try:
        alerts = WeatherAlert.query.filter(
            WeatherAlert.location.in_(locations),
            WeatherAlert.is_official.is_(True),
            WeatherAlert.source == 'QWeather',
            WeatherAlert.starts_at <= now,
            WeatherAlert.ends_at >= now,
        ).order_by(WeatherAlert.alert_date.desc()).all()
        return alerts
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.warning('公开预警读取失败，显示待确认状态')
        return []


def configured_miniprogram_qr():
    """仅接受管理员提供的本地静态图片，不生成或推断小程序码。"""
    filename = current_app.config.get('MINIPROGRAM_QR_STATIC_PATH')
    if not filename:
        legacy = current_app.config.get('WX_MINIPROGRAM_ACTION_CODE_IMAGE', '')
        filename = legacy[7:] if isinstance(legacy, str) and legacy.startswith('static/') else None
    if not isinstance(filename, str) or not filename.strip():
        return None
    static_root = Path(current_app.static_folder).resolve()
    candidate = (static_root / filename).resolve()
    if not candidate.is_relative_to(static_root):
        return None
    if candidate.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'} or not candidate.is_file():
        return None
    return candidate.relative_to(static_root).as_posix()


def public_warning_state(snapshot, alerts, now=None):
    """依据预警来源回执显示获取状态，数据库空列表不代表无预警。"""
    now = now or utcnow()
    source_status = snapshot.get('source_status') or {}
    state = source_status.get('warnings') if isinstance(source_status, dict) else None
    state = state if isinstance(state, dict) else {}

    def parse_time(value):
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo is not None else None
        except (TypeError, ValueError):
            return None

    fetched = parse_time(state.get('fetched_at'))
    expires = parse_time(state.get('expires_at'))
    result = {
        'status': 'unknown',
        'message': ('最新预警获取状态待确认，当前展示 %d 条有效预警。' % len(alerts)) if alerts else '官方预警数据暂未获取。',
        'fetched_at': fetched.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M（北京时间）') if fetched else None,
    }
    success = (state.get('available') is True
               and state.get('provider') == 'QWeather'
               and state.get('status') in ('ok', 'success'))
    if not success:
        return result
    if not fetched or not expires or fetched > now or expires <= fetched:
        result['message'] = '官方预警更新时间待确认。'
        return result
    if expires <= now:
        result.update(status='expired', message='官方预警数据已过期，等待更新。')
    elif not alerts and snapshot.get('warnings') == []:
        result.update(status='none', message='本次查询：暂无官方预警。')
    else:
        result.update(status='available', message=f'预警数据已获取，当前展示 {len(alerts)} 条有效预警。')
    return result
