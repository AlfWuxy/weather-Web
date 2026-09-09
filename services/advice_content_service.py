# -*- coding: utf-8 -*-
"""医生科普与短提醒模板：草稿 → 审核/发布 → 更新或撤回。"""
from __future__ import annotations

import secrets

from core.db_models import AdviceContent, User
from core.extensions import db
from core.time_utils import ensure_utc_aware, utcnow
from utils.validators import sanitize_input


FORBIDDEN_CLAIM_MARKERS = (
    '电费不到',
    '一天电费',
    '停药',
    '换药',
    '疾病概率',
    '保证安全',
    '一定不会',
    '统一喝',
    '必须喝',
)


class AdviceContentError(Exception):
    def __init__(self, code, message, status_code=400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


def _public_id():
    return secrets.token_hex(16)


def _require_doctor(user):
    if user is None or getattr(user, 'role', None) != 'doctor':
        raise AdviceContentError('forbidden', '只有医生本人可以起草、审核或发布科普内容。', 403)
    return user


def _scan_forbidden(text):
    body = text or ''
    for marker in FORBIDDEN_CLAIM_MARKERS:
        if marker in body:
            return marker
    return None


def serialize_advice(row, *, viewer=None):
    doctor_attributed = bool(
        row.status == 'published'
        and row.reviewer_user_id
        and row.reviewer_user_id == row.author_user_id
    )
    reviewer = db.session.get(User, row.reviewer_user_id) if row.reviewer_user_id else None
    if reviewer is not None and getattr(reviewer, 'role', None) != 'doctor':
        doctor_attributed = False
    now = utcnow()
    valid = True
    if row.valid_from and ensure_utc_aware(row.valid_from) > now:
        valid = False
    if row.valid_until and ensure_utc_aware(row.valid_until) < now:
        valid = False
    return {
        'id': row.public_id,
        'kind': row.kind,
        'status': row.status,
        'title': row.title,
        'body': row.body,
        'source': row.source,
        'scenario': row.scenario,
        'version': row.version,
        'author_user_id': row.author_user_id,
        'reviewer_user_id': row.reviewer_user_id,
        'published_at': row.published_at.isoformat() if row.published_at else None,
        'valid_from': row.valid_from.isoformat() if row.valid_from else None,
        'valid_until': row.valid_until.isoformat() if row.valid_until else None,
        'doctor_attributed': doctor_attributed,
        'attribution_label': '徐医生已审核' if doctor_attributed else '平台规则提醒',
        'currently_valid': bool(row.status == 'published' and valid),
    }


def create_draft(user, *, kind, title, body, source, scenario='heat', parent_id=None, valid_until=None):
    _require_doctor(user)
    kind = (kind or '').strip()
    if kind not in {'science', 'template'}:
        raise AdviceContentError('invalid_kind', '内容类型只能是科普或短提醒模板。')
    scenario = (scenario or 'heat').strip() or 'heat'
    title = sanitize_input(title, max_length=160) or ''
    source = sanitize_input(source, max_length=200) or ''
    body = (body or '').strip()
    if not title or not body or not source:
        raise AdviceContentError('missing_fields', '标题、正文和来源都需要填写。')
    forbidden = _scan_forbidden(body)
    if forbidden:
        raise AdviceContentError('forbidden_claim', f'正文含有未核实承诺或超出范围的表述（{forbidden}），不能进入草稿。')
    version = 1
    if parent_id:
        parent = AdviceContent.query.filter_by(public_id=parent_id).first()
        if not parent:
            raise AdviceContentError('not_found', '原内容不存在。', 404)
        version = int(parent.version or 1) + 1
        parent_pk = parent.id
    else:
        parent_pk = None
    now = utcnow()
    row = AdviceContent(
        public_id=_public_id(),
        kind=kind,
        status='draft',
        title=title,
        body=body,
        source=source,
        scenario=scenario,
        version=version,
        author_user_id=user.id,
        parent_id=parent_pk,
        created_at=now,
        updated_at=now,
        valid_until=valid_until,
    )
    db.session.add(row)
    db.session.flush()
    return row


def publish_advice(user, public_id):
    _require_doctor(user)
    row = AdviceContent.query.filter_by(public_id=public_id).first()
    if not row:
        raise AdviceContentError('not_found', '内容不存在。', 404)
    if row.status not in {'draft', 'withdrawn'}:
        raise AdviceContentError('invalid_transition', '只有草稿或已撤回内容可以发布。', 409)
    if getattr(user, 'role', None) != 'doctor':
        raise AdviceContentError('forbidden', '管理员不能以医生身份发布或审核。', 403)
    forbidden = _scan_forbidden(row.body)
    if forbidden:
        raise AdviceContentError('forbidden_claim', f'正文含有未核实承诺（{forbidden}），不能发布。')
    now = utcnow()
    row.status = 'published'
    row.reviewer_user_id = user.id
    row.published_at = now
    row.valid_from = now
    row.updated_at = now
    db.session.flush()
    return row


def withdraw_advice(user, public_id):
    _require_doctor(user)
    row = AdviceContent.query.filter_by(public_id=public_id).first()
    if not row:
        raise AdviceContentError('not_found', '内容不存在。', 404)
    if row.status != 'published':
        raise AdviceContentError('invalid_transition', '只有已发布内容可以撤回。', 409)
    row.status = 'withdrawn'
    row.updated_at = utcnow()
    db.session.flush()
    return row


def active_templates(scenario='heat'):
    now = utcnow()
    rows = AdviceContent.query.filter_by(kind='template', status='published', scenario=scenario).all()
    valid = []
    for row in rows:
        if row.valid_from and ensure_utc_aware(row.valid_from) > now:
            continue
        if row.valid_until and ensure_utc_aware(row.valid_until) < now:
            continue
        if not row.reviewer_user_id:
            continue
        valid.append(row)
    valid.sort(key=lambda item: item.published_at or item.updated_at or now, reverse=True)
    return valid


def list_advice(user, *, status=None):
    _require_doctor(user)
    query = AdviceContent.query
    if status:
        query = query.filter_by(status=status)
    return query.order_by(AdviceContent.updated_at.desc()).all()
