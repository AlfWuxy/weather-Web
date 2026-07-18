# -*- coding: utf-8 -*-
"""和风天气月度调用预算保护。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import logging
import os
import threading
import time

from flask import current_app, has_app_context, has_request_context

from core.time_utils import now_local
from utils.parsers import parse_bool, parse_int

logger = logging.getLogger(__name__)

DEFAULT_MONTHLY_LIMIT = 40000
_REDIS_BUDGET_PREFIX = "qweather:budget:app:v2"
_REDIS_CLIENT_EXTENSION_KEY = "qweather_budget_redis_client"
_LOCAL_LOCK = threading.Lock()
_LOCAL_TOTALS = defaultdict(int)
_LOCAL_ENDPOINTS = defaultdict(lambda: defaultdict(int))
_BLOCKED_LOGGED = set()


def _config_value(key, default=None):
    if has_app_context():
        return current_app.config.get(key, default)
    return os.getenv(key, default)


def _monthly_limit():
    return max(
        parse_int(
            _config_value("QWEATHER_MONTHLY_REQUEST_LIMIT", DEFAULT_MONTHLY_LIMIT),
            default=DEFAULT_MONTHLY_LIMIT,
        ),
        0,
    )


def _redis_url():
    value = (
        _config_value("WEATHER_CACHE_REDIS_URL", "")
        or _config_value("REDIS_URL", "")
        or ""
    )
    return str(value).strip()


def _fail_closed():
    return parse_bool(
        _config_value("QWEATHER_BUDGET_FAIL_CLOSED", "1"),
        default=True,
    )


def _log_network_gate_blocked_once(reason):
    """记录网络闸门状态，不输出原始配置值。"""
    key = ("network-gate", reason)
    if key in _BLOCKED_LOGGED:
        return
    _BLOCKED_LOGGED.add(key)
    if reason == "invalid":
        logger.error("和风天气网络闸门配置无效，已按关闭策略阻断请求。")
        return
    if reason == "request-context":
        logger.warning("普通 HTTP 请求禁止直连和风天气，已按只读策略阻断请求。")
        return
    logger.warning("和风天气网络闸门尚未开放，已阻断请求。")


def _network_gate_allows_request(now_epoch=None):
    """按 Unix 秒闸门决定是否允许访问和风天气网络。"""
    raw_value = _config_value("QWEATHER_NETWORK_NOT_BEFORE_EPOCH", "")
    if raw_value is None:
        return True
    value = str(raw_value).strip()
    if not value:
        return True

    try:
        # bool 是 int 的子类，但不应被当作合法的 Unix 秒配置。
        if isinstance(raw_value, bool):
            raise ValueError("boolean epoch is invalid")
        not_before_epoch = int(value, 10)
        if not_before_epoch < 0:
            raise ValueError("negative epoch is invalid")
    except (TypeError, ValueError, OverflowError):
        _log_network_gate_blocked_once("invalid")
        return False

    current_epoch = time.time() if now_epoch is None else float(now_epoch)
    if current_epoch < not_before_epoch:
        _log_network_gate_blocked_once("future")
        return False
    return True


def _month_key(now=None):
    now = now or now_local()
    return now.strftime("%Y-%m")


def _seconds_until_expiry(now=None):
    """预算键保留到下月开始后两天，便于跨月审计。"""
    now = now or now_local()
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1)
    else:
        next_month = datetime(now.year, now.month + 1, 1)
    return max(int((next_month - now).total_seconds()) + 2 * 86400, 86400)


def _redis_budget_keys(month):
    """使用应用专属键，供应商控制台历史总量不得写入这里。"""
    prefix = f"{_REDIS_BUDGET_PREFIX}:{month}"
    return f"{prefix}:total", f"{prefix}:endpoints"


def get_qweather_redis_client():
    """返回预算与短期天气缓存共用的 Redis 客户端。"""
    if not has_app_context():
        return None
    cached = current_app.extensions.get(_REDIS_CLIENT_EXTENSION_KEY)
    if cached is not None:
        return cached
    redis_url = _redis_url()
    if not redis_url:
        return None
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(redis_url, decode_responses=True)
    except Exception as exc:
        logger.warning("和风天气预算 Redis 初始化失败: %s", exc)
        return None
    current_app.extensions[_REDIS_CLIENT_EXTENSION_KEY] = client
    return client


def _log_blocked_once(month, limit, backend):
    key = (month, backend)
    if key in _BLOCKED_LOGGED:
        return
    _BLOCKED_LOGGED.add(key)
    logger.error(
        "和风天气月度调用保护已生效: month=%s limit=%s backend=%s，后续请求将使用备用源。",
        month,
        limit,
        backend,
    )


def _reserve_local(month, endpoint, limit):
    with _LOCAL_LOCK:
        if _LOCAL_TOTALS[month] >= limit:
            _log_blocked_once(month, limit, "local")
            return False
        _LOCAL_TOTALS[month] += 1
        _LOCAL_ENDPOINTS[month][endpoint] += 1
        return True


def reserve_qweather_request(endpoint):
    """在发出一次和风请求前预占月度额度。"""
    # 普通 HTTP 请求只允许读取已落地缓存，且阻断时不得消耗预算。
    if has_request_context():
        _log_network_gate_blocked_once("request-context")
        return False

    # 网络闸门必须先于 Redis 和进程内计数，阻断期不消耗任何预算。
    if not _network_gate_allows_request():
        return False

    endpoint = str(endpoint or "unknown").strip() or "unknown"
    limit = _monthly_limit()
    if limit <= 0:
        _log_blocked_once(_month_key(), limit, "disabled")
        return False

    now = now_local()
    month = _month_key(now)
    client = get_qweather_redis_client()
    redis_configured = bool(_redis_url())
    if client is not None:
        total_key, endpoint_key = _redis_budget_keys(month)
        try:
            count = int(client.incr(total_key))
            if count == 1:
                ttl = _seconds_until_expiry(now)
                client.expire(total_key, ttl)
            if count > limit:
                try:
                    client.decr(total_key)
                except Exception:
                    pass
                _log_blocked_once(month, limit, "redis")
                return False
            client.hincrby(endpoint_key, endpoint, 1)
            if count == 1:
                client.expire(endpoint_key, ttl)
            return True
        except Exception as exc:
            logger.error("和风天气预算 Redis 计数失败: %s", exc)
            if redis_configured and _fail_closed():
                _log_blocked_once(month, limit, "redis-unavailable")
                return False

    if redis_configured and _fail_closed():
        _log_blocked_once(month, limit, "redis-unavailable")
        return False

    return _reserve_local(month, endpoint, limit)


def get_qweather_budget_snapshot():
    """返回当前月预算快照，供运维核验使用。"""
    now = now_local()
    month = _month_key(now)
    limit = _monthly_limit()
    client = get_qweather_redis_client()
    if client is not None:
        try:
            total_key, endpoint_key = _redis_budget_keys(month)
            total = int(client.get(total_key) or 0)
            endpoints = client.hgetall(endpoint_key) or {}
            return {
                "month": month,
                "limit": limit,
                "used": total,
                "remaining": max(limit - total, 0),
                "backend": "redis",
                "endpoints": {key: int(value) for key, value in endpoints.items()},
            }
        except Exception as exc:
            logger.warning("读取和风天气预算快照失败: %s", exc)

    with _LOCAL_LOCK:
        total = int(_LOCAL_TOTALS.get(month, 0))
        endpoints = dict(_LOCAL_ENDPOINTS.get(month, {}))
    return {
        "month": month,
        "limit": limit,
        "used": total,
        "remaining": max(limit - total, 0),
        "backend": "local",
        "endpoints": endpoints,
    }
