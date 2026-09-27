# -*- coding: utf-8 -*-
"""网站侧终端最小联动。事件名只反映设备能力，不把按键记成防护行动。"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from core.db_models import CareDevice, DeviceEvent, Pair
from core.extensions import db
from core.security import hash_identifier
from core.time_utils import ensure_utc_aware, utcnow
from services.advice_content_service import active_templates, serialize_advice
from services.family_access import require_pair_access
from utils.validators import sanitize_input

DEVICE_EVENT_NAMES = frozenset({
    'heartbeat',
    'button_pressed',
    'tts_command_sent',
    'sos_hold',
    'content_pulled',
})

DEVICE_ONLINE_SECONDS = 180


class DeviceLinkError(Exception):
    def __init__(self, code, message, status_code=400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


def _public_id():
    return secrets.token_hex(16)


def _hash_token(token):
    return hash_identifier(str(token or '').strip())


def _parse_device_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return ensure_utc_aware(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def authorize_device(user, pair, *, label='home-terminal'):
    require_pair_access(user, pair, 'manage')
    plain = secrets.token_urlsafe(32)
    now = utcnow()
    device = CareDevice(
        public_id=_public_id(),
        pair_id=pair.id,
        label=sanitize_input(label, max_length=80) or 'home-terminal',
        token_hash=_hash_token(plain),
        authorized_at=now,
        created_by_user_id=user.id,
        created_at=now,
    )
    db.session.add(device)
    db.session.flush()
    return device, plain


def revoke_device(user, public_id):
    device = CareDevice.query.filter_by(public_id=public_id).first()
    if not device:
        raise DeviceLinkError('not_found', '设备不存在。', 404)
    pair = db.session.get(Pair, device.pair_id)
    require_pair_access(user, pair, 'manage')
    if device.revoked_at:
        return device
    device.revoked_at = utcnow()
    db.session.flush()
    return device


def device_by_token(plain):
    token_hash = _hash_token(plain)
    device = CareDevice.query.filter_by(token_hash=token_hash).first()
    if not device or device.revoked_at:
        return None
    return device


def device_online(device, *, now=None):
    now = now or utcnow()
    last = ensure_utc_aware(device.last_heartbeat_at or device.last_seen_at)
    if not last:
        return False
    return (now - last).total_seconds() <= DEVICE_ONLINE_SECONDS


def serialize_device(device, *, include_status=True):
    online = device_online(device)
    payload = {
        'id': device.public_id,
        'pair_id': device.pair_id,
        'label': device.label,
        'authorized_at': device.authorized_at.isoformat() if device.authorized_at else None,
        'revoked': bool(device.revoked_at),
        'verification_mode': 'simulated',
    }
    if include_status:
        payload.update({
            'online': online,
            'last_heartbeat_at': device.last_heartbeat_at.isoformat() if device.last_heartbeat_at else None,
            'last_seen_at': device.last_seen_at.isoformat() if device.last_seen_at else None,
            'unconfirmed': (not online) and not device.revoked_at,
        })
    return payload


def pull_content(device):
    now = utcnow()
    device.last_seen_at = now
    templates = active_templates('heat')
    items = []
    for row in templates:
        data = serialize_advice(row)
        items.append({
            'content_id': data['id'],
            'version': data['version'],
            'scenario': data['scenario'],
            'body': data['body'],
            'source': data['source'],
            'doctor_attributed': data['doctor_attributed'],
            'attribution_label': data['attribution_label'],
            'valid_until': data['valid_until'],
        })
    return {
        'verification_mode': 'simulated',
        'server_time': now.isoformat(),
        'pair_id': device.pair_id,
        'templates': items,
        'notes': [
            '本接口不返回姓名、疾病或联系方式。',
            '拉取内容不等于老人听到。',
            '真实联调需对应固件、设备与日志证据。',
        ],
    }


def report_event(device, *, client_event_id, event_name, device_occurred_at=None, payload=None):
    event_name = sanitize_input(event_name, max_length=40) or ''
    if event_name not in DEVICE_EVENT_NAMES:
        raise DeviceLinkError('invalid_event_name', '事件名必须符合设备真实能力。')
    client_event_id = sanitize_input(client_event_id, max_length=64) or ''
    if not client_event_id:
        raise DeviceLinkError('missing_client_event_id', '需要设备侧唯一事件编号。')
    existing = DeviceEvent.query.filter_by(device_id=device.id, client_event_id=client_event_id).first()
    now = utcnow()
    device.last_seen_at = now
    if event_name == 'heartbeat':
        device.last_heartbeat_at = now
    if existing:
        return existing, False
    if payload is not None and not isinstance(payload, dict):
        raise DeviceLinkError('invalid_payload', '附加数据必须是对象。')
    row = DeviceEvent(
        device_id=device.id,
        client_event_id=client_event_id,
        event_name=event_name,
        device_occurred_at=_parse_device_time(device_occurred_at),
        server_received_at=now,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
        created_at=now,
    )
    db.session.add(row)
    db.session.flush()
    return row, True
