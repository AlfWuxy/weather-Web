# -*- coding: utf-8 -*-
"""Minimal i18n helpers for error messages."""
from __future__ import annotations

from typing import Dict


_DEFAULT_MESSAGES: Dict[str, Dict[str, str]] = {
    "zh": {
        "unknown_error": "发生未知错误",
        "csrf_invalid": "CSRF 校验失败",
        "invalid_input": "输入参数无效",
        "not_found": "资源不存在",
        "permission_denied": "权限不足",
    },
    "en": {
        "unknown_error": "Unknown error occurred",
        "csrf_invalid": "Invalid CSRF token",
        "invalid_input": "Invalid input",
        "not_found": "Resource not found",
        "permission_denied": "Permission denied",
    },
}


def get_error_message(key: str, lang: str = "zh") -> str:
    """Return an error message for a given key and language."""
    messages = _DEFAULT_MESSAGES.get(lang, _DEFAULT_MESSAGES["zh"])
    return messages.get(key, messages.get("unknown_error", "Unknown error"))
