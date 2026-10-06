# -*- coding: utf-8 -*-
"""公开县级信息的呈现边界，不读取家庭健康档案。"""
from pathlib import Path

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
