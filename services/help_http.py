# -*- coding: utf-8 -*-
"""求助 HTTP 适配：统一错误码与 request_id。"""
from __future__ import annotations

from flask import g, jsonify, request

from services.family_access import FamilyAccessError
from services.help_request_service import HelpRequestError


def error_payload(code, message, status_code, extra=None):
    body = {
        'success': False,
        'error': code,
        'message': message,
        'request_id': getattr(g, 'request_id', None),
    }
    if extra:
        body.update(extra)
    return jsonify(body), status_code


def handle_domain_error(exc):
    if isinstance(exc, (HelpRequestError, FamilyAccessError)):
        extra = getattr(exc, 'extra', None)
        return error_payload(exc.code, exc.message, exc.status_code, extra)
    return error_payload('service_unavailable', '服务暂时不可用。', 503)


def json_body():
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise HelpRequestError('invalid_payload', '请求格式无效。', 400)
    return payload
