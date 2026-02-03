# -*- coding: utf-8 -*-
"""Notification helpers."""
from datetime import datetime
import logging

from flask import current_app

from core.extensions import db
from core.db_models import Notification
from core.time_utils import today_local_start_utc, today_local_end_utc
from utils.parsers import json_or_none

logger = logging.getLogger(__name__)


def _notification_daily_count(user_id):
    """统计今日通知数量（基于本地时区的"今天"）"""
    try:
        # 使用 UTC-aware 的本地日期边界，正确过滤 UTC 时间戳
        start_utc = today_local_start_utc()
        end_utc = today_local_end_utc()
        return Notification.query.filter(
            Notification.user_id == user_id,
            Notification.created_at >= start_utc,
            Notification.created_at <= end_utc
        ).count()
    except Exception:
        db.session.rollback()
        return 0


def create_notification(user_id, title, message, level='info', category='general', member_id=None, action_url=None, meta=None):
    """创建站内通知（受Feature Flag控制）"""
    if not current_app.config.get('FEATURE_NOTIFICATIONS'):
        return None
    if not user_id or isinstance(user_id, str):
        return None
    max_daily = current_app.config.get('NOTIFICATION_MAX_DAILY', 5)
    if max_daily and _notification_daily_count(user_id) >= max_daily:
        return None
    try:
        notification = Notification(
            user_id=user_id,
            member_id=member_id,
            category=category,
            title=title,
            message=message,
            level=level,
            action_url=action_url,
            meta=json_or_none(meta or {})
        )
        db.session.add(notification)
        db.session.commit()
        return notification
    except Exception as exc:
        logger.warning("通知写入失败: %s", exc)
        db.session.rollback()
        return None
