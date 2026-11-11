# -*- coding: utf-8 -*-
"""Security audit logging helpers."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from flask import current_app, g, has_app_context, request

logger = logging.getLogger(__name__)


def _log_audit_failure(exc: BaseException) -> None:
    detail = type(exc).__name__
    try:
        from utils.log_safety import sanitize_log_text
        detail = sanitize_log_text(detail)
    except Exception:
        pass
    logger.warning("审计日志写入失败: %s", detail)


def log_security_event(
    action: str,
    *,
    actor_id: Optional[int] = None,
    actor_role: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    extra_data: Optional[Dict[str, Any]] = None,
):
    """Record a security-relevant event (log and optional DB persistence)."""
    payload = {
        "action": action,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "request_id": getattr(g, "request_id", None),
    }
    logger.info("SECURITY_EVENT %s", json.dumps(payload, ensure_ascii=True))

    if not has_app_context() or not current_app.config.get("FEATURE_AUDIT_LOGS"):
        return

    try:
        from core.audit import _get_client_ip_context, persist_audit_entry

        ip_ctx = _get_client_ip_context()
        extra_payload = extra_data.copy() if isinstance(extra_data, dict) else {}
        extra_payload.setdefault("ip_source", ip_ctx.get("ip_source"))
        extra_payload.setdefault("via_trusted_proxy", bool(ip_ctx.get("via_trusted_proxy")))
        if ip_ctx.get("ip_prefix"):
            extra_payload.setdefault("ip_prefix", ip_ctx.get("ip_prefix"))

        # 先收成纯标量。只读路径里的 rollback 会使已加载对象过期。
        persist_audit_entry({
            "actor_id": actor_id,
            "actor_role": actor_role,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "extra_data": extra_payload,
            "ip_address": ip_ctx.get("ip_hash"),
            "user_agent": request.headers.get("User-Agent"),
            "request_id": getattr(g, "request_id", None),
        })
    except Exception as exc:
        _log_audit_failure(exc)
