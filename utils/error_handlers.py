# -*- coding: utf-8 -*-
"""Centralized API error handling helpers."""
from __future__ import annotations

import json
import logging
from typing import Optional, Tuple

from flask import current_app, jsonify, request

logger = logging.getLogger(__name__)

DEFAULT_ERROR_MESSAGE = "服务暂时不可用，请稍后再试"


def classify_exception(exc: Exception) -> Tuple[int, str]:
    """Classify exceptions into HTTP status codes and safe messages."""
    if isinstance(exc, FileNotFoundError):
        return 404, "资源不存在"
    if isinstance(exc, json.JSONDecodeError):
        return 400, "JSON 解析失败"
    if isinstance(exc, KeyError):
        return 400, "缺少必要字段"
    if isinstance(exc, ValueError):
        return 400, "参数错误"
    if isinstance(exc, TypeError):
        return 400, "参数类型错误"
    if isinstance(exc, PermissionError):
        return 403, "权限不足"
    if isinstance(exc, TimeoutError):
        return 504, "请求超时"
    if isinstance(exc, OSError):
        return 500, "系统资源异常"
    if isinstance(exc, RuntimeError):
        return 500, "服务运行异常"
    return 500, DEFAULT_ERROR_MESSAGE


def json_error_response(error: str, status_code: int):
    """Stable JSON envelope used by Mini Program and JSON APIs."""
    response = jsonify({"success": False, "error": error})
    response.status_code = status_code
    return response


def rate_limit_exceeded_response(exc):
    """JSON 429 for API paths; keep Limiter's default body for HTML routes."""
    path = request.path or ""
    if path.startswith("/mp/api/") or path.startswith("/api/"):
        return json_error_response("rate_limited", 429)
    get_response = getattr(exc, "get_response", None)
    if callable(get_response):
        return get_response()
    return json_error_response("rate_limited", 429)


def handle_api_exception(
    exc: Exception,
    context_msg: str,
    *,
    log: Optional[logging.Logger] = None,
    include_details: Optional[bool] = None,
    status_code: Optional[int] = None,
):
    """Handle API exceptions with consistent logging and responses."""
    active_logger = log or logger
    active_logger.exception(context_msg)

    if include_details is None:
        include_details = current_app.config.get("DEBUG", False)

    default_status, message = classify_exception(exc)
    status = status_code or default_status

    payload = {
        "success": False,
        "error": message,
    }
    if include_details:
        payload["error_detail"] = str(exc)
        payload["error_type"] = type(exc).__name__

    response = jsonify(payload)
    response.status_code = status
    return response
