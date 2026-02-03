# -*- coding: utf-8 -*-
"""Audit helpers."""
import logging

from flask import current_app, g, request
from flask_login import current_user

from core.extensions import db
from core.db_models import AuditLog
from core.guest import is_guest_user
from utils.parsers import json_or_none

logger = logging.getLogger(__name__)


def log_audit(action, resource_type=None, resource_id=None, metadata=None):
    """记录审计日志（受Feature Flag控制）"""
    if not current_app.config.get('FEATURE_AUDIT_LOGS'):
        return None
    try:
        actor_id = None
        actor_role = None
        if current_user.is_authenticated:
            actor_id = current_user.id if not is_guest_user(current_user) else None
            actor_role = getattr(current_user, 'role', None)
        entry = AuditLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            extra_data=json_or_none(metadata or {}),
            ip_address=request.headers.get('X-Forwarded-For', request.remote_addr),
            user_agent=request.headers.get('User-Agent', '')[:200],
            request_id=getattr(g, 'request_id', None)
        )
        db.session.add(entry)
        db.session.commit()
        return entry
    except Exception as exc:
        logger.warning("审计日志写入失败: %s", exc)
        db.session.rollback()
        return None
