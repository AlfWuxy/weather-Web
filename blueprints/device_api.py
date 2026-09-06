# -*- coding: utf-8 -*-
"""网站侧终端接口。真实联调需固件证据；本轮提供模拟验证。"""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from core.db_models import Pair
from core.extensions import db, limiter
from core.security import reject_guest
from services.device_link_service import (
    DeviceLinkError,
    authorize_device,
    device_by_token,
    pull_content,
    report_event,
    revoke_device,
    serialize_device,
)
from services.family_access import FamilyAccessError
from services.help_http import handle_domain_error

bp = Blueprint('device_api', __name__)


def _device_token():
    auth = request.headers.get('Authorization') or ''
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return (request.headers.get('X-Device-Token') or '').strip()


def _require_device():
    device = device_by_token(_device_token())
    if not device:
        return None, (jsonify({'success': False, 'error': 'unauthorized', 'verification_mode': 'simulated'}), 401)
    return device, None


@bp.route('/api/v1/devices', methods=['POST'], endpoint='api_v1_devices_create')
@login_required
@reject_guest
def api_v1_devices_create():
    payload = request.get_json(silent=True) or {}
    try:
        pair_id = int(payload.get('pair_id') or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'invalid_pair_id'}), 400
    pair = db.session.get(Pair, pair_id)
    try:
        device, plain = authorize_device(current_user, pair, label=payload.get('label') or 'home-terminal')
        db.session.commit()
    except (FamilyAccessError, DeviceLinkError) as exc:
        db.session.rollback()
        return handle_domain_error(exc)
    data = serialize_device(device)
    data['device_token'] = plain
    data['token_note'] = '明文只显示一次，请保存在设备侧。'
    return jsonify({'success': True, 'data': data, 'verification_mode': 'simulated'})


@bp.route('/api/v1/devices/<public_id>/revoke', methods=['POST'], endpoint='api_v1_devices_revoke')
@login_required
@reject_guest
def api_v1_devices_revoke(public_id):
    try:
        device = revoke_device(current_user, public_id)
        db.session.commit()
    except (FamilyAccessError, DeviceLinkError) as exc:
        db.session.rollback()
        return handle_domain_error(exc)
    return jsonify({'success': True, 'data': serialize_device(device), 'verification_mode': 'simulated'})


@bp.route('/device/api/v1/content', methods=['GET'], endpoint='device_content')
@limiter.limit('60 per minute')
def device_content():
    device, error = _require_device()
    if error:
        return error
    try:
        payload = pull_content(device)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return handle_domain_error(exc)
    return jsonify({'success': True, 'data': payload})


@bp.route('/device/api/v1/events', methods=['POST'], endpoint='device_events')
@limiter.limit('120 per minute')
def device_events():
    device, error = _require_device()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    try:
        row, created = report_event(
            device,
            client_event_id=payload.get('client_event_id'),
            event_name=payload.get('event_name'),
            device_occurred_at=payload.get('device_occurred_at'),
            payload=payload.get('payload') if isinstance(payload.get('payload'), dict) else None,
        )
        db.session.commit()
    except DeviceLinkError as exc:
        db.session.rollback()
        return handle_domain_error(exc)
    return jsonify({
        'success': True,
        'data': {
            'accepted': True,
            'created': created,
            'event_name': row.event_name,
            'client_event_id': row.client_event_id,
            'device_occurred_at': row.device_occurred_at.isoformat() if row.device_occurred_at else None,
            'server_received_at': row.server_received_at.isoformat() if row.server_received_at else None,
            'not_action_completed': True,
            'verification_mode': 'simulated',
        },
    })
