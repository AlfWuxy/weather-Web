# -*- coding: utf-8 -*-
"""Time helpers for consistent local date handling.

时区处理最佳实践：
1. 数据库时间戳字段（created_at, updated_at 等）应使用 UTC timezone-aware datetime
   - 写入时使用：utcnow()
   - 查询时使用：utcnow(), today_local_start_utc(), today_local_end_utc(), date_to_utc_start(), date_to_utc_end()

2. 用户界面显示时间时，使用本地时间 naive datetime（用于模板渲染）
   - 使用：now_local(), today_local()

3. 数据库日期字段（entry_date 等 Date 类型）可以直接使用 date 对象
   - 使用：today_local()

4. 避免混用 naive 和 aware datetime 进行比较或运算
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

DEFAULT_TIMEZONE = 'Asia/Shanghai'
logger = logging.getLogger(__name__)


def _resolve_timezone():
    tz_name = DEFAULT_TIMEZONE
    if has_app_context():
        tz_name = current_app.config.get('APP_TIMEZONE', DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:
        logger.warning("时区解析失败，已回退到默认时区(%s): %s", DEFAULT_TIMEZONE, exc)
        try:
            return ZoneInfo(DEFAULT_TIMEZONE)
        except Exception as fallback_exc:
            logger.warning("默认时区解析失败，已回退到 UTC: %s", fallback_exc)
            return ZoneInfo('UTC')


def now_local():
    tz = _resolve_timezone()
    return datetime.now(tz).replace(tzinfo=None)


def today_local():
    return now_local().date()


def utcnow():
    """返回 timezone-aware 的 UTC 时间（替代已废弃的 datetime.utcnow()）

    推荐在数据库时间戳中使用此函数，确保时区信息完整。
    如需存储 naive datetime，使用 utcnow_naive()。
    """
    return datetime.now(timezone.utc)


def utcnow_naive():
    """返回 naive UTC 时间（仅用于需要 naive datetime 的旧代码兼容）

    注意：仅在必须保持 naive datetime 的场景下使用（如某些数据库列）。
    新代码应优先使用 timezone-aware 的 utcnow()。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_local_start_utc():
    """返回本地日期的开始时刻，以 UTC timezone-aware datetime 形式返回

    用于数据库查询时过滤 UTC 时间戳字段。
    例如：Notification.created_at >= today_local_start_utc()
    """
    tz = _resolve_timezone()
    local_now = datetime.now(tz)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc)


def today_local_end_utc():
    """返回本地日期的结束时刻，以 UTC timezone-aware datetime 形式返回

    用于数据库查询时过滤 UTC 时间戳字段。
    例如：Notification.created_at <= today_local_end_utc()
    """
    tz = _resolve_timezone()
    local_now = datetime.now(tz)
    local_end = local_now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return local_end.astimezone(timezone.utc)


def local_datetime_to_utc(local_dt):
    """将本地时间 datetime 转换为 UTC timezone-aware datetime

    - 若输入为 aware，则直接转换到 UTC
    - 若输入为 naive，假定其语义为本地时区
    """
    if local_dt is None:
        return None
    if local_dt.tzinfo is not None:
        return local_dt.astimezone(timezone.utc)
    tz = _resolve_timezone()
    local_aware = tz.localize(local_dt) if hasattr(tz, 'localize') else local_dt.replace(tzinfo=tz)
    return local_aware.astimezone(timezone.utc)


def date_to_utc_start(local_date):
    """将本地日期转换为当天开始时刻的 UTC timezone-aware datetime

    参数:
        local_date: date 对象，表示本地时区的某一天

    返回:
        该日期在本地时区的 00:00:00，转换为 UTC 的 timezone-aware datetime

    用于数据库查询时过滤 UTC 时间戳字段。
    例如：WeatherAlert.alert_date >= date_to_utc_start(start_date)
    """
    local_dt = datetime.combine(local_date, datetime.min.time())
    return local_datetime_to_utc(local_dt)


def date_to_utc_end(local_date):
    """将本地日期转换为当天结束时刻的 UTC timezone-aware datetime

    参数:
        local_date: date 对象，表示本地时区的某一天

    返回:
        该日期在本地时区的 23:59:59.999999，转换为 UTC 的 timezone-aware datetime

    用于数据库查询时过滤 UTC 时间戳字段。
    例如：WeatherAlert.alert_date <= date_to_utc_end(end_date)
    """
    local_dt = datetime.combine(local_date, datetime.max.time())
    return local_datetime_to_utc(local_dt)


def ensure_utc_aware(dt):
    """确保 datetime 对象是 UTC timezone-aware 的

    参数:
        dt: datetime 对象（可能是 naive 或 aware）

    返回:
        UTC timezone-aware datetime

    行为:
        - 如果已经是 aware（有 tzinfo），转换为 UTC
        - 如果是 naive（无 tzinfo），假定为 UTC 并添加 tzinfo

    用于从数据库读取的 datetime 字段，确保可以安全地与 utcnow() 比较/相减。
    例如：ensure_utc_aware(record.created_at) - utcnow()
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        # 已经是 aware，转换为 UTC
        return dt.astimezone(timezone.utc)
    # 是 naive，假定为 UTC 并添加 tzinfo
    return dt.replace(tzinfo=timezone.utc)


def utc_to_local_date(dt):
    """将 UTC 时间戳转换为本地日期（date）

    - 支持 naive/aware datetime
    - naive 按 UTC 语义处理
    """
    if dt is None:
        return None
    aware = ensure_utc_aware(dt)
    tz = _resolve_timezone()
    return aware.astimezone(tz).date()
