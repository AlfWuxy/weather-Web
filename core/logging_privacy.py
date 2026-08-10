# -*- coding: utf-8 -*-
"""微信正式运行态的全局日志隐私边界。"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from typing import Any, Mapping


_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
_PREVIOUS_RECORD_FACTORY = None
_PREVIOUS_MAKE_RECORD = None

_STANDARD_RECORD_KEYS = frozenset({
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
})
_INTERNAL_SAFE_PAYLOAD = "_formal_privacy_payload"
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SAFE_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SAFE_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_SAFE_ROLES = frozenset({"admin", "user", "caregiver", "community", "guest"})
_SAFE_EXTERNAL_SERVICES = frozenset({
    "ai_chat",
    "openmeteo_forecast_daily",
    "openmeteo_now",
    "openmeteo_now_hourly",
    "openmeteo_nowcast_hourly",
    "qweather_air_v1",
    "qweather_daily_for_now",
    "qweather_forecast",
    "qweather_forecast_only",
    "qweather_hourly_for_now",
    "qweather_now",
    "qweather_weatheralert_v1",
    "wxpusher_send",
})


class FormalRequestLogEvent:
    """只携带经过验证的请求日志字段，字符串形式同样安全。"""

    __slots__ = ("payload",)

    def __init__(self, payload: Mapping[str, Any] | None):
        self.payload = _sanitize_request_payload(payload)

    def __str__(self):
        return json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))


def formal_request_log_event(payload: Mapping[str, Any] | None):
    """构造正式运行态专用请求事件。"""
    return FormalRequestLogEvent(payload)


def sanitize_request_path(path: Any):
    """只保留无查询参数的安全路径，并遮蔽行动凭据。"""
    if not isinstance(path, str):
        return "/"
    candidate = path.split("?", 1)[0].split("#", 1)[0]
    if not candidate.startswith("/") or any(ord(char) < 32 for char in candidate):
        return "/"
    candidate = candidate[:256]
    for prefix in ("/e/", "/t/"):
        if not candidate.startswith(prefix):
            continue
        remainder = candidate[len(prefix):]
        suffix = ""
        if "/" in remainder:
            _, tail = remainder.split("/", 1)
            suffix = f"/{tail[:128]}"
        return f"{prefix}<token>{suffix}"
    return candidate


def install_formal_logging_privacy(enabled: bool | None = None):
    """在正式微信运行态安装幂等的全局 LogRecord 净化层。"""
    if enabled is None:
        enabled = (os.getenv("WECHAT_FORMAL_RUNTIME") or "").strip() == "1"
    if not enabled:
        return False

    global _INSTALLED, _PREVIOUS_RECORD_FACTORY, _PREVIOUS_MAKE_RECORD
    with _INSTALL_LOCK:
        if _INSTALLED:
            return True
        _PREVIOUS_RECORD_FACTORY = logging.getLogRecordFactory()
        _PREVIOUS_MAKE_RECORD = logging.Logger.makeRecord
        logging.setLogRecordFactory(_formal_record_factory)
        logging.Logger.makeRecord = _formal_make_record
        _INSTALLED = True
    return True


def _restore_logging_privacy_for_testing():
    """仅供单元测试恢复进程级 logging 状态。"""
    global _INSTALLED, _PREVIOUS_RECORD_FACTORY, _PREVIOUS_MAKE_RECORD
    with _INSTALL_LOCK:
        if not _INSTALLED:
            return
        if logging.getLogRecordFactory() is _formal_record_factory:
            logging.setLogRecordFactory(_PREVIOUS_RECORD_FACTORY)
        if logging.Logger.makeRecord is _formal_make_record:
            logging.Logger.makeRecord = _PREVIOUS_MAKE_RECORD
        _PREVIOUS_RECORD_FACTORY = None
        _PREVIOUS_MAKE_RECORD = None
        _INSTALLED = False


def _formal_record_factory(*args, **kwargs):
    record = _PREVIOUS_RECORD_FACTORY(*args, **kwargs)
    return _sanitize_record(record)


def _formal_make_record(logger, *args, **kwargs):
    # Logger.makeRecord 会在工厂返回后写入 extra，因此这里再次裁剪。
    record = _PREVIOUS_MAKE_RECORD(logger, *args, **kwargs)
    return _sanitize_record(record)


def _sanitize_record(record):
    payload = getattr(record, _INTERNAL_SAFE_PAYLOAD, None)
    if not isinstance(payload, dict):
        payload = _base_metadata(record)
        if isinstance(record.msg, FormalRequestLogEvent):
            # 事件对象可能被调用方保留引用，记录创建时必须重新执行白名单校验。
            payload.update(_sanitize_request_payload(record.msg.payload))

    safe_message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    record.msg = safe_message
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    record.name = payload["logger"]
    record.levelname = payload["level"]
    record.module = payload["module"]
    record.funcName = payload["function"]
    record.lineno = payload["line"]
    # 防止 handler 的自定义格式重新暴露文件系统路径。
    record.pathname = payload["module"]
    record.filename = payload["module"]
    record.threadName = "thread"
    record.processName = "process"
    if hasattr(record, "taskName"):
        record.taskName = None

    for key in tuple(record.__dict__):
        if key not in _STANDARD_RECORD_KEYS and key != _INTERNAL_SAFE_PAYLOAD:
            record.__dict__.pop(key, None)
    record.__dict__[_INTERNAL_SAFE_PAYLOAD] = payload
    return record


def _base_metadata(record):
    return {
        "event": "python_log",
        "logger": _safe_code_name(getattr(record, "name", None), "unknown", 96),
        "level": _safe_level(getattr(record, "levelname", None)),
        "module": _safe_code_name(getattr(record, "module", None), "unknown", 64),
        "function": _safe_code_name(getattr(record, "funcName", None), "unknown", 96),
        "line": _safe_int(getattr(record, "lineno", None), minimum=0, maximum=10_000_000) or 0,
    }


def _sanitize_request_payload(payload):
    payload = payload if isinstance(payload, Mapping) else {}
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not _SAFE_REQUEST_ID_RE.fullmatch(request_id):
        request_id = None

    user_id = _safe_int(payload.get("user_id"), minimum=1, maximum=9_223_372_036_854_775_807)
    user_role = payload.get("user_role")
    user_role = user_role if isinstance(user_role, str) and user_role in _SAFE_ROLES else None

    method = payload.get("method")
    method = method if isinstance(method, str) and method in _SAFE_METHODS else "UNKNOWN"

    endpoint = payload.get("endpoint")
    endpoint = _safe_optional_code_name(endpoint, 128)
    status = _safe_int(payload.get("status"), minimum=100, maximum=599)
    duration_ms = _safe_number(payload.get("duration_ms"), minimum=0.0, maximum=86_400_000.0)

    return {
        "event": "http_request",
        "request_id": request_id,
        "user_id": user_id,
        "user_role": user_role,
        "method": method,
        "path": sanitize_request_path(payload.get("path")),
        "endpoint": endpoint,
        "status": status,
        "duration_ms": duration_ms,
        "external_api": _sanitize_external_api(payload.get("external_api")),
    }


def _sanitize_external_api(value):
    if not isinstance(value, (list, tuple)):
        return []
    safe_items = []
    for item in value[:16]:
        if not isinstance(item, Mapping):
            continue
        service = item.get("service")
        if not isinstance(service, str) or service not in _SAFE_EXTERNAL_SERVICES:
            continue
        safe_items.append({
            "service": service,
            "elapsed_ms": _safe_number(
                item.get("elapsed_ms"),
                minimum=0.0,
                maximum=86_400_000.0,
            ),
            "status": _safe_int(item.get("status"), minimum=100, maximum=599),
        })
    return safe_items


def _safe_code_name(value, fallback, maximum):
    if not isinstance(value, str) or not value or len(value) > maximum:
        return fallback
    return value if _SAFE_NAME_RE.fullmatch(value) else fallback


def _safe_optional_code_name(value, maximum):
    if value is None:
        return None
    return _safe_code_name(value, None, maximum)


def _safe_level(value):
    if not isinstance(value, str):
        return "UNKNOWN"
    candidate = value.upper()
    if candidate in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        return candidate
    return "UNKNOWN"


def _safe_int(value, *, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _safe_number(value, *, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    candidate = float(value)
    if not math.isfinite(candidate) or not minimum <= candidate <= maximum:
        return None
    return round(candidate, 2)
