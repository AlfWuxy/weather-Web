# -*- coding: utf-8 -*-
"""Security audit logging helpers."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from flask import current_app, g, has_app_context, request

logger = logging.getLogger(__name__)


def _serialize_extra(extra_data: Optional[Dict[str, Any]]) -> str:
    if not extra_data:
        return "{}"
    try:
        return json.dumps(extra_data, ensure_ascii=True)
    except (TypeError, ValueError):
        return "{}"


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
        from core.db_models import AuditLog
        from core.extensions import db

        entry = AuditLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            extra_data=_serialize_extra(extra_data),
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
            user_agent=request.headers.get("User-Agent"),
            request_id=getattr(g, "request_id", None),
        )
        db.session.add(entry)
    except Exception as exc:
        logger.warning("Failed to persist audit log: %s", exc)
