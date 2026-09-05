# -*- coding: utf-8 -*-
"""家庭空间授权：邀请、角色、对象级权限。所有渠道共用。"""
from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from core.db_models import FamilyInvite, FamilyMembership, FamilySpace, Pair, User
from core.extensions import db
from core.security import hash_identifier
from core.time_utils import ensure_utc_aware, utcnow

ROLE_OWNER = 'owner'
ROLE_CAREGIVER = 'caregiver'
ROLE_ELDER_PROXY = 'elder_proxy'
ROLE_COMMUNITY_LIMITED = 'community_limited'

ACTIVE = 'active'
REVOKED = 'revoked'
LEFT = 'left'

INVITABLE_ROLES = frozenset({ROLE_CAREGIVER, ROLE_ELDER_PROXY, ROLE_COMMUNITY_LIMITED})
ALL_ROLES = INVITABLE_ROLES | {ROLE_OWNER}

# 权限矩阵：读对象、发起求助、接收/处理、结案、邀请、撤销
CAN_READ = ALL_ROLES
CAN_CREATE_HELP = frozenset({ROLE_OWNER, ROLE_ELDER_PROXY})
CAN_ACK = frozenset({ROLE_OWNER, ROLE_CAREGIVER, ROLE_COMMUNITY_LIMITED})
CAN_RESOLVE = frozenset({ROLE_OWNER, ROLE_CAREGIVER, ROLE_COMMUNITY_LIMITED})
CAN_CANCEL = frozenset({ROLE_OWNER, ROLE_CAREGIVER, ROLE_ELDER_PROXY})
CAN_INVITE = frozenset({ROLE_OWNER})
CAN_MANAGE_SPACE = frozenset({ROLE_OWNER})


class FamilyAccessError(Exception):
    """稳定错误码，供 HTTP 层映射。"""

    def __init__(self, code, message, status_code=400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


def _public_id():
    return secrets.token_hex(16)


def _invite_hash(plain):
    return hash_identifier(str(plain or '').strip())


def active_membership(user_id, family_space_id):
    if not user_id or not family_space_id:
        return None
    return FamilyMembership.query.filter_by(
        user_id=user_id,
        family_space_id=family_space_id,
        status=ACTIVE,
    ).first()


def _require_role(user_id, family_space_id, allowed):
    membership = active_membership(user_id, family_space_id)
    if not membership or membership.role not in allowed:
        raise FamilyAccessError('forbidden', '没有权限执行该操作。', 403)
    return membership


def ensure_space_for_pair(pair, *, actor_user_id=None, commit=False):
    """把既有 Pair 映射到家庭空间，保留 caregiver 管理权。不静默合并用户。"""
    if pair is None:
        raise FamilyAccessError('not_found', '照护对象不存在。', 404)
    if pair.family_space_id:
        space = db.session.get(FamilySpace, pair.family_space_id)
        if space:
            return space
    now = utcnow()
    space = FamilySpace(
        public_id=_public_id(),
        name='家庭照护',
        created_by_user_id=pair.caregiver_id,
        is_test=bool(getattr(pair, 'is_test', False)),
        created_at=now,
    )
    db.session.add(space)
    db.session.flush()
    owner = FamilyMembership(
        family_space_id=space.id,
        user_id=pair.caregiver_id,
        role=ROLE_OWNER,
        status=ACTIVE,
        invited_by_user_id=None,
        created_at=now,
    )
    db.session.add(owner)
    pair.family_space_id = space.id
    db.session.flush()
    if commit:
        db.session.commit()
    return space


def visible_pair_ids_for_user(user_id):
    """服务端计算可见 Pair，不信任客户端 family_id。"""
    rows = (
        db.session.query(Pair.id)
        .join(FamilyMembership, FamilyMembership.family_space_id == Pair.family_space_id)
        .filter(
            FamilyMembership.user_id == user_id,
            FamilyMembership.status == ACTIVE,
            Pair.status == 'active',
            Pair.family_space_id.isnot(None),
        )
        .all()
    )
    ids = {row[0] for row in rows}
    # 尚未回填家庭空间的旧 Pair：仅原 caregiver 可见
    legacy = Pair.query.filter_by(caregiver_id=user_id, status='active').filter(
        Pair.family_space_id.is_(None)
    ).all()
    for pair in legacy:
        ids.add(pair.id)
    return ids


def can_access_pair(user, pair, action):
    if user is None or pair is None:
        return False
    if getattr(user, 'role', None) == 'admin':
        return True
    allowed = {
        'read': CAN_READ,
        'create_help': CAN_CREATE_HELP,
        'ack': CAN_ACK,
        'resolve': CAN_RESOLVE,
        'cancel': CAN_CANCEL,
        'invite': CAN_INVITE,
        'manage': CAN_MANAGE_SPACE,
    }.get(action)
    if not allowed:
        return False
    space_id = pair.family_space_id
    if not space_id:
        if pair.caregiver_id == user.id:
            if action == 'create_help':
                return True
            if action in {'read', 'ack', 'resolve', 'cancel', 'invite', 'manage'}:
                return True
        return False
    membership = active_membership(user.id, space_id)
    if not membership:
        return False
    if membership.role == ROLE_COMMUNITY_LIMITED:
        authorized = (getattr(user, 'authorized_community', None) or '').strip()
        if not authorized or authorized != (pair.community_code or '').strip():
            return False
    return membership.role in allowed


def require_pair_access(user, pair, action):
    if not can_access_pair(user, pair, action):
        # 无权对象统一 404，避免枚举
        raise FamilyAccessError('not_found', '对象不存在或无权访问。', 404)
    return True


def create_invite(user, pair_or_space, role, *, ttl_hours=72, max_uses=1):
    if role not in INVITABLE_ROLES:
        raise FamilyAccessError('invalid_role', '邀请角色无效。', 400)
    if isinstance(pair_or_space, Pair):
        space = ensure_space_for_pair(pair_or_space)
        require_pair_access(user, pair_or_space, 'invite')
    else:
        space = pair_or_space
        _require_role(user.id, space.id, CAN_INVITE)
    max_uses = int(max_uses or 1)
    if max_uses < 1 or max_uses > 20:
        raise FamilyAccessError('invalid_max_uses', '邀请次数超出范围。', 400)
    ttl_hours = int(ttl_hours or 72)
    if ttl_hours < 1 or ttl_hours > 24 * 14:
        raise FamilyAccessError('invalid_ttl', '邀请有效期超出范围。', 400)
    plain = secrets.token_urlsafe(16)
    invite = FamilyInvite(
        family_space_id=space.id,
        code_hash=_invite_hash(plain),
        role=role,
        expires_at=utcnow() + timedelta(hours=ttl_hours),
        max_uses=max_uses,
        use_count=0,
        created_by_user_id=user.id,
        created_at=utcnow(),
    )
    db.session.add(invite)
    db.session.flush()
    return invite, plain


def preview_invite(plain_code):
    """GET 预览：不消费。"""
    invite = FamilyInvite.query.filter_by(code_hash=_invite_hash(plain_code)).first()
    if not invite:
        raise FamilyAccessError('not_found', '邀请不存在或已失效。', 404)
    space = db.session.get(FamilySpace, invite.family_space_id)
    status = _invite_status(invite)
    pairs = Pair.query.filter_by(family_space_id=invite.family_space_id, status='active').all()
    return {
        'family_space_id': space.public_id if space else None,
        'family_name': space.name if space else '',
        'role': invite.role,
        'status': status,
        'expires_at': invite.expires_at.isoformat() if invite.expires_at else None,
        'remaining_uses': max(0, (invite.max_uses or 0) - (invite.use_count or 0)),
        'pair_count': len(pairs),
        'consumes': False,
    }


def _invite_status(invite):
    now = utcnow()
    if invite.revoked_at:
        return 'revoked'
    if invite.expires_at and ensure_utc_aware(invite.expires_at) < now:
        return 'expired'
    if (invite.use_count or 0) >= (invite.max_uses or 0):
        return 'exhausted'
    return 'active'


def consume_invite(user, plain_code):
    """确认兑换。事务内完成；并发最多一人成功。"""
    if user is None:
        raise FamilyAccessError('unauthorized', '请先登录。', 401)
    code_hash = _invite_hash(plain_code)
    invite = FamilyInvite.query.filter_by(code_hash=code_hash).first()
    if not invite:
        raise FamilyAccessError('not_found', '邀请不存在或已失效。', 404)
    status = _invite_status(invite)
    if status != 'active':
        raise FamilyAccessError('invite_inactive', '邀请已失效，无法加入。', 409)
    existing = active_membership(user.id, invite.family_space_id)
    if existing:
        raise FamilyAccessError('already_member', '你已在该家庭中。', 409)
    now = utcnow()
    membership = FamilyMembership(
        family_space_id=invite.family_space_id,
        user_id=user.id,
        role=invite.role,
        status=ACTIVE,
        invited_by_user_id=invite.created_by_user_id,
        created_at=now,
    )
    try:
        with db.session.begin_nested():
            claimed = (
                FamilyInvite.query.filter(
                    FamilyInvite.id == invite.id,
                    FamilyInvite.revoked_at.is_(None),
                    FamilyInvite.expires_at > now,
                    FamilyInvite.use_count < FamilyInvite.max_uses,
                ).update(
                    {
                        FamilyInvite.use_count: FamilyInvite.use_count + 1,
                        FamilyInvite.last_consumed_at: now,
                    },
                    synchronize_session='fetch',
                )
            )
            if claimed != 1:
                raise FamilyAccessError('invite_inactive', '邀请已失效，无法加入。', 409)
            db.session.add(membership)
            db.session.flush()
    except IntegrityError as exc:
        raise FamilyAccessError('already_member', '你已在该家庭中。', 409) from exc
    invite = db.session.get(FamilyInvite, invite.id)
    return membership, invite


def revoke_invite(user, invite_id):
    invite = db.session.get(FamilyInvite, invite_id)
    if not invite:
        raise FamilyAccessError('not_found', '邀请不存在。', 404)
    _require_role(user.id, invite.family_space_id, CAN_INVITE)
    if not invite.revoked_at:
        invite.revoked_at = utcnow()
        db.session.flush()
    return invite


def leave_space(user, family_space_id):
    membership = active_membership(user.id, family_space_id)
    if not membership:
        raise FamilyAccessError('not_found', '你不在该家庭中。', 404)
    if membership.role == ROLE_OWNER:
        others = FamilyMembership.query.filter_by(
            family_space_id=family_space_id,
            status=ACTIVE,
        ).filter(FamilyMembership.user_id != user.id).count()
        if others:
            raise FamilyAccessError(
                'owner_must_transfer',
                '请先指定其他管理人再退出家庭。',
                409,
            )
    membership.status = LEFT
    membership.revoked_at = utcnow()
    db.session.flush()
    return membership


def revoke_membership(actor, membership_id):
    membership = db.session.get(FamilyMembership, membership_id)
    if not membership:
        raise FamilyAccessError('not_found', '成员不存在。', 404)
    _require_role(actor.id, membership.family_space_id, CAN_MANAGE_SPACE)
    if membership.role == ROLE_OWNER:
        raise FamilyAccessError('cannot_revoke_owner', '不能撤销管理人。', 409)
    membership.status = REVOKED
    membership.revoked_at = utcnow()
    db.session.flush()
    return membership
