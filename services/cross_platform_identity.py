# -*- coding: utf-8 -*-
"""网页账号、未验证手机号与微信小程序身份的安全串联。"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from contextlib import ExitStack
from datetime import timedelta

from flask import current_app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from core.db_models import (
    AlertDelivery,
    ApiToken,
    AuditLog,
    Debrief,
    FamilyMember,
    HealthDiary,
    HealthRiskAssessment,
    MedicationReminder,
    MiniProgramIdentity,
    MiniProgramLinkChallenge,
    MiniProgramSession,
    Notification,
    Pair,
    PairLink,
    UsageEvent,
    User,
    WxpusherBindingChallenge,
)
from core.extensions import db
from core.time_utils import ensure_utc_aware, utcnow
from services.miniprogram_auth import (
    acquire_miniprogram_identity_lock,
    issue_miniprogram_session,
)
from services.push.locks import push_owner_lock
from utils.audit_log import log_security_event


logger = logging.getLogger(__name__)
PHONE_SEPARATORS_RE = re.compile(r"[ \t().-]+", re.ASCII)
CHINA_MOBILE_RE = re.compile(r"1[3-9][0-9]{9}", re.ASCII)
CHINA_E164_RE = re.compile(r"\+861[3-9][0-9]{9}", re.ASCII)
E164_RE = re.compile(r"\+[1-9][0-9]{7,14}", re.ASCII)
LINK_CODE_RE = re.compile(r"[0-9]{8}", re.ASCII)
INTERNAL_USERNAME_PREFIXES = (
    "wx_",
    "retired_wx_",
    "deleted_mp_",
)


class AccountLinkError(RuntimeError):
    """向前端公开稳定错误码，内部原因只进入审计日志。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


def normalize_phone(value, *, required: bool = False):
    """把中国大陆手机号或标准 E.164 号码归一化，不推断号码所有权。"""
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError("请输入手机号。")
        return None

    compact = PHONE_SEPARATORS_RE.sub("", raw)
    if compact.startswith("0086"):
        compact = f"+86{compact[4:]}"
    elif compact.startswith("86") and len(compact) == 13:
        compact = f"+{compact}"
    elif CHINA_MOBILE_RE.fullmatch(compact):
        compact = f"+86{compact}"

    if not E164_RE.fullmatch(compact):
        raise ValueError("手机号格式不正确，请填写 11 位大陆手机号或带国家码的号码。")
    if compact.startswith("+86") and not CHINA_E164_RE.fullmatch(compact):
        raise ValueError("中国大陆手机号格式不正确，请填写 11 位有效号码。")
    return compact


def is_reserved_internal_username(value) -> bool:
    """系统账号命名空间不能由网页注册或管理员分配给普通账号。"""
    normalized = str(value or "").strip().casefold()
    return any(
        normalized.startswith(prefix)
        for prefix in INTERNAL_USERNAME_PREFIXES
    )


def phone_username_candidates(phone_normalized):
    """返回会与大陆手机号登录入口发生歧义的纯数字用户名。"""
    normalized = str(phone_normalized or "")
    if CHINA_E164_RE.fullmatch(normalized):
        local = normalized[3:]
        return {local, f"86{local}", f"0086{local}"}
    return set()


def _link_code_pepper() -> str:
    pepper = str(current_app.config.get("ACCOUNT_LINK_CODE_PEPPER") or "").strip()
    if not pepper:
        raise AccountLinkError(
            "account_link_not_configured",
            "跨端账号绑定尚未配置完整，请联系管理员。",
            503,
        )
    return pepper


def _hash_link_code(code: str) -> str:
    return hmac.new(
        _link_code_pepper().encode("utf-8"),
        str(code).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _link_ttl_seconds() -> int:
    try:
        value = int(current_app.config.get("ACCOUNT_LINK_CODE_TTL_SECONDS", 600))
    except (TypeError, ValueError):
        value = 600
    return max(300, min(value, 1800))


def create_account_link_challenge(user_id: int) -> dict:
    """撤销旧挑战并生成一次性八位数字码，明文只返回本次调用。"""
    user = db.session.get(User, int(user_id))
    if user is None or user.deleted_at is not None:
        raise AccountLinkError("account_unavailable", "账号已失效，请重新登录。", 401)

    now = utcnow()
    MiniProgramLinkChallenge.query.filter(
        MiniProgramLinkChallenge.user_id == user.id,
        MiniProgramLinkChallenge.consumed_at.is_(None),
        MiniProgramLinkChallenge.revoked_at.is_(None),
    ).update(
        {MiniProgramLinkChallenge.revoked_at: now},
        synchronize_session=False,
    )

    challenge = None
    plain_code = None
    for _attempt in range(8):
        candidate = f"{secrets.randbelow(90_000_000) + 10_000_000:08d}"
        candidate_hash = _hash_link_code(candidate)
        if MiniProgramLinkChallenge.query.filter_by(
            code_hash=candidate_hash
        ).first() is not None:
            continue
        plain_code = candidate
        challenge = MiniProgramLinkChallenge(
            user_id=user.id,
            code_hash=candidate_hash,
            created_at=now,
            expires_at=now + timedelta(seconds=_link_ttl_seconds()),
            auth_version_at_create=int(user.auth_version),
        )
        db.session.add(challenge)
        break
    if challenge is None or plain_code is None:
        db.session.rollback()
        raise AccountLinkError(
            "account_link_create_failed",
            "绑定码生成失败，请稍后重试。",
            503,
        )

    log_security_event(
        "miniprogram_link_challenge_created",
        actor_id=user.id,
        actor_role=user.role,
        resource_type="miniprogram_link_challenge",
        extra_data={"ttl_seconds": _link_ttl_seconds()},
    )
    try:
        db.session.commit()
    except IntegrityError as exc:
        # 部分唯一索引确保并发生成后最多保留一个有效挑战。
        db.session.rollback()
        raise AccountLinkError(
            "account_link_create_conflict",
            "刚刚已经生成过绑定码，请刷新页面后使用最新结果。",
            409,
        ) from exc
    return {
        "code": plain_code,
        "expires_at": ensure_utc_aware(challenge.expires_at).isoformat(),
        "expires_in": _link_ttl_seconds(),
    }


def _temporary_user_has_private_data(user: User) -> bool:
    """只允许空白微信临时账号转移，避免静默合并两套健康和家庭数据。"""
    if user.account_origin != "miniprogram_placeholder":
        return True
    if any(
        (
            user.age is not None,
            bool(user.gender),
            bool(user.community),
            bool(user.has_chronic_disease),
            bool(user.chronic_diseases),
            bool(user.email),
            bool(user.phone_normalized),
            bool(user.wxpusher_uid),
            bool(user.push_enabled),
        )
    ):
        return True

    ownership_checks = (
        FamilyMember.query.filter_by(user_id=user.id),
        HealthDiary.query.filter_by(user_id=user.id),
        MedicationReminder.query.filter_by(user_id=user.id),
        HealthRiskAssessment.query.filter_by(user_id=user.id),
        Notification.query.filter_by(user_id=user.id),
        PairLink.query.filter_by(caregiver_id=user.id),
        Pair.query.filter_by(caregiver_id=user.id),
        Debrief.query.filter_by(owner_user_id=user.id),
        ApiToken.query.filter_by(user_id=user.id),
        AlertDelivery.query.filter_by(user_id=user.id),
    )
    return any(query.with_entities(db.literal(1)).first() is not None for query in ownership_checks)


def _link_failure_policy():
    try:
        max_failures = int(current_app.config.get("ACCOUNT_LINK_FAILURE_MAX", 5))
    except (TypeError, ValueError):
        max_failures = 5
    try:
        window_seconds = int(
            current_app.config.get("ACCOUNT_LINK_FAILURE_WINDOW_SECONDS", 600)
        )
    except (TypeError, ValueError):
        window_seconds = 600
    return (
        max(3, min(max_failures, 10)),
        max(300, min(window_seconds, 1800)),
    )


def _identity_link_locked(identity, now):
    locked_until = (
        ensure_utc_aware(identity.link_locked_until)
        if identity.link_locked_until is not None
        else None
    )
    return locked_until is not None and locked_until > now


def _anonymize_temporary_source_user(source_user: User, now) -> None:
    """绑定完成后切断临时微信占位账号与新账号之间的可关联痕迹。"""
    source_user_id = int(source_user.id)

    UsageEvent.query.filter_by(user_id=source_user_id).update(
        {UsageEvent.user_id: None},
        synchronize_session=False,
    )
    MiniProgramLinkChallenge.query.filter_by(user_id=source_user_id).delete(
        synchronize_session=False,
    )
    WxpusherBindingChallenge.query.filter_by(user_id=source_user_id).delete(
        synchronize_session=False,
    )
    AlertDelivery.query.filter_by(reviewed_by_user_id=source_user_id).update(
        {
            AlertDelivery.reviewed_by_user_id: None,
            AlertDelivery.reviewed_at: None,
            AlertDelivery.review_action: None,
        },
        synchronize_session=False,
    )
    AuditLog.query.filter_by(actor_id=source_user_id).update(
        {
            AuditLog.actor_id: None,
            AuditLog.actor_role: "anonymous_miniprogram_identity",
            AuditLog.resource_type: None,
            AuditLog.resource_id: None,
            AuditLog.extra_data: None,
            AuditLog.ip_address: None,
            AuditLog.user_agent: None,
            AuditLog.request_id: None,
        },
        synchronize_session=False,
    )

    random_username = f"retired_wx_{secrets.token_hex(16)}"
    while User.query.filter_by(username=random_username).first() is not None:
        random_username = f"retired_wx_{secrets.token_hex(16)}"
    source_user.username = random_username
    source_user.email = None
    source_user.phone_normalized = None
    source_user.phone_verified_at = None
    source_user.account_origin = "retired_miniprogram"
    source_user.role = "user"
    source_user.auth_version = int(source_user.auth_version or 1) + 1
    source_user.created_at = now
    source_user.last_login = None
    source_user.deleted_at = now
    source_user.age = None
    source_user.gender = None
    source_user.community = None
    source_user.has_chronic_disease = False
    source_user.chronic_diseases = None
    source_user.wxpusher_uid = None
    source_user.wxpusher_uid_verified_at = None
    source_user.push_enabled = False
    source_user.wxpusher_consent_version = None
    source_user.wxpusher_consented_at = None
    source_user.health_sensitive_consent_version = None
    source_user.health_sensitive_consented_at = None
    source_user.set_password(secrets.token_urlsafe(48))


def _record_identity_link_failure(identity, now, reason):
    """按微信身份累计失败，审计只记录原因类别，不记录绑定码。"""
    max_failures, window_seconds = _link_failure_policy()
    first_failed_at = (
        ensure_utc_aware(identity.link_first_failed_at)
        if identity.link_first_failed_at is not None
        else None
    )
    if (
        first_failed_at is None
        or now - first_failed_at > timedelta(seconds=window_seconds)
    ):
        identity.link_failed_count = 0
        identity.link_first_failed_at = now
        identity.link_locked_until = None
    identity.link_failed_count = int(identity.link_failed_count or 0) + 1
    if identity.link_failed_count >= max_failures:
        identity.link_locked_until = now + timedelta(seconds=window_seconds)
    log_security_event(
        "miniprogram_link_challenge_rejected",
        actor_id=identity.user_id,
        actor_role="user",
        resource_type="miniprogram_identity",
        resource_id=str(identity.id),
        extra_data={
            "reason": reason,
            "locked": bool(identity.link_locked_until),
        },
    )
    db.session.commit()


def _read_link_owner_ids(
    *,
    identity_id: int,
    authenticated_user_id: int,
    code_hash: str | None,
) -> tuple[int, int | None]:
    """锁前只读取 owner 主键，随后主动结束陈旧读事务。"""
    identity_row = db.session.execute(
        db.select(
            MiniProgramIdentity.id,
            MiniProgramIdentity.user_id,
        ).where(MiniProgramIdentity.id == int(identity_id))
    ).one_or_none()
    target_user_id = None
    if code_hash:
        target_user_id = db.session.execute(
            db.select(MiniProgramLinkChallenge.user_id).where(
                MiniProgramLinkChallenge.code_hash == code_hash
            )
        ).scalar_one_or_none()
    db.session.rollback()

    if (
        identity_row is None
        or int(identity_row.user_id) != int(authenticated_user_id)
    ):
        raise AccountLinkError(
            "miniprogram_session_required",
            "请先使用微信登录，再绑定网页账号。",
            403,
        )
    return (
        int(identity_row.user_id),
        int(target_user_id) if target_user_id is not None else None,
    )


def _lock_link_identity(
    identity_id: int,
    identity_openid_hash: str | None,
):
    """在 owner 文件锁内取得身份事务锁并重新读取 identity。"""
    dialect_name = db.engine.dialect.name
    if dialect_name == "sqlite":
        # 文件锁已经固定 owner 顺序；BEGIN IMMEDIATE 再取得 SQLite 全库写锁。
        db.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    elif dialect_name == "postgresql":
        normalized_openid_hash = str(identity_openid_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_openid_hash):
            raise AccountLinkError(
                "miniprogram_session_required",
                "请重新使用微信登录后再绑定网页账号。",
                403,
            )
        acquire_miniprogram_identity_lock(normalized_openid_hash)

    identity_query = db.select(MiniProgramIdentity).where(
        MiniProgramIdentity.id == int(identity_id)
    )
    if dialect_name != "sqlite":
        identity_query = identity_query.with_for_update()
    return db.session.execute(
        identity_query.execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _locked_link_users(owner_user_ids):
    """按固定主键顺序取得 owner 行并覆盖 Session 中的旧对象。"""
    normalized_ids = sorted({int(user_id) for user_id in owner_user_ids})
    query = (
        db.select(User)
        .where(User.id.in_(normalized_ids))
        .order_by(User.id)
        .execution_options(populate_existing=True)
    )
    if db.engine.dialect.name != "sqlite":
        query = query.with_for_update()
    return {
        int(user.id): user
        for user in db.session.execute(query).scalars()
    }


def _locked_other_identity_for_target(
    *,
    target_user_id: int,
    current_identity_id: int,
):
    """锁定目标账号上的其他微信身份，供改密后的安全替换流程使用。"""
    query = db.select(MiniProgramIdentity).where(
        MiniProgramIdentity.user_id == int(target_user_id),
        MiniProgramIdentity.id != int(current_identity_id),
    )
    if db.engine.dialect.name != "sqlite":
        query = query.with_for_update()
    return db.session.execute(
        query.execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _remove_stale_target_identity(
    identity: MiniProgramIdentity,
    target_user: User,
) -> None:
    """仅移除已被网页改密失效的旧微信映射，让新微信凭新短码接管。"""
    if int(identity.binding_auth_version or 0) == int(
        target_user.auth_version or 0
    ):
        raise AccountLinkError(
            "target_already_linked",
            "该网页账号已经绑定其他微信，请联系管理员确认。",
            409,
        )

    MiniProgramSession.query.filter_by(identity_id=identity.id).delete(
        synchronize_session=False,
    )
    MiniProgramLinkChallenge.query.filter_by(
        consumed_identity_id=identity.id,
    ).update(
        {MiniProgramLinkChallenge.consumed_identity_id: None},
        synchronize_session=False,
    )
    db.session.delete(identity)
    db.session.flush()


def consume_account_link_challenge(
    *,
    code: str,
    identity_id: int,
    authenticated_user_id: int,
    identity_openid_hash: str | None = None,
) -> dict:
    """原子消费短码，把空白微信身份长期关联到网页账号并轮换会话。"""
    normalized_code = str(code or "").replace(" ", "").strip()
    code_hash = (
        _hash_link_code(normalized_code)
        if LINK_CODE_RE.fullmatch(normalized_code)
        else None
    )
    try:
        source_user_id, target_user_id = _read_link_owner_ids(
            identity_id=identity_id,
            authenticated_user_id=authenticated_user_id,
            code_hash=code_hash,
        )
        locked_owner_ids = {source_user_id}
        if target_user_id is not None:
            locked_owner_ids.add(target_user_id)

        with ExitStack() as owner_locks:
            for owner_user_id in sorted(locked_owner_ids):
                owner_locks.enter_context(push_owner_lock(owner_user_id))

            # 等待锁的时间也计入挑战有效期，所有时效判断以真正进入临界区为准。
            now = utcnow()
            identity = _lock_link_identity(identity_id, identity_openid_hash)
            if (
                identity is None
                or int(identity.user_id) != source_user_id
                or int(identity.user_id) != int(authenticated_user_id)
            ):
                raise AccountLinkError(
                    "miniprogram_session_required",
                    "请先使用微信登录，再绑定网页账号。",
                    403,
                )
            if (
                identity_openid_hash
                and not hmac.compare_digest(
                    str(identity.openid_hash or ""),
                    str(identity_openid_hash),
                )
            ):
                raise AccountLinkError(
                    "miniprogram_session_required",
                    "请重新使用微信登录后再绑定网页账号。",
                    403,
                )
            if _identity_link_locked(identity, now):
                raise AccountLinkError(
                    "account_link_temporarily_locked",
                    "绑定尝试次数过多，请 10 分钟后再试。",
                    429,
                )

            users_by_id = _locked_link_users(locked_owner_ids)
            source_user = users_by_id.get(source_user_id)
            if source_user is None or source_user.deleted_at is not None:
                raise AccountLinkError(
                    "account_unavailable",
                    "当前账号已失效。",
                    401,
                )

            if code_hash is None:
                _record_identity_link_failure(identity, now, "invalid_format")
                raise AccountLinkError(
                    "invalid_link_code",
                    "绑定码无效或已过期，请在网页重新生成。",
                    400,
                )

            challenge_query = db.select(MiniProgramLinkChallenge).where(
                MiniProgramLinkChallenge.code_hash == code_hash
            )
            if db.engine.dialect.name != "sqlite":
                challenge_query = challenge_query.with_for_update()
            challenge = db.session.execute(
                challenge_query.execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if (
                challenge is None
                or target_user_id is None
                or int(challenge.user_id) != target_user_id
            ):
                _record_identity_link_failure(identity, now, "not_found")
                raise AccountLinkError(
                    "invalid_link_code",
                    "绑定码无效或已过期，请在网页重新生成。",
                    400,
                )

            target_user = users_by_id.get(target_user_id)
            if target_user is None or target_user.deleted_at is not None:
                raise AccountLinkError(
                    "target_account_unavailable",
                    "网页账号已失效。",
                    409,
                )
            challenge_invalid = (
                challenge.revoked_at is not None
                or challenge.consumed_at is not None
                or ensure_utc_aware(challenge.expires_at) <= now
                or int(challenge.auth_version_at_create)
                != int(target_user.auth_version)
            )
            if challenge_invalid:
                if (
                    challenge.consumed_at is None
                    and challenge.revoked_at is None
                ):
                    challenge.revoked_at = now
                _record_identity_link_failure(identity, now, "inactive")
                raise AccountLinkError(
                    "invalid_link_code",
                    "绑定码无效或已过期，请在网页重新生成。",
                    400,
                )
            other_identity = _locked_other_identity_for_target(
                target_user_id=target_user.id,
                current_identity_id=identity.id,
            )
            if other_identity is not None:
                _remove_stale_target_identity(
                    other_identity,
                    target_user,
                )
            if (
                source_user.id != target_user.id
                and _temporary_user_has_private_data(source_user)
            ):
                raise AccountLinkError(
                    "source_account_has_data",
                    "当前微信账号已有照护数据，系统不会自动合并。请先联系管理员处理。",
                    409,
                )

            claimed = db.session.execute(
                update(MiniProgramLinkChallenge)
                .where(
                    MiniProgramLinkChallenge.id == challenge.id,
                    MiniProgramLinkChallenge.consumed_at.is_(None),
                    MiniProgramLinkChallenge.revoked_at.is_(None),
                    MiniProgramLinkChallenge.expires_at > now,
                )
                .values(
                    consumed_at=now,
                    consumed_identity_id=identity.id,
                    attempt_count=MiniProgramLinkChallenge.attempt_count + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise AccountLinkError(
                    "link_code_race_lost",
                    "绑定码已被使用，请在网页重新生成。",
                    409,
                )

            # 复合外键没有 ON UPDATE CASCADE，先删除本微信身份的旧会话再改 owner。
            for old_session in MiniProgramSession.query.filter_by(
                identity_id=identity.id
            ).all():
                db.session.delete(old_session)
            db.session.flush()
            source_was_temporary = source_user.id != target_user.id
            if source_was_temporary:
                identity.user_id = target_user.id
                _anonymize_temporary_source_user(source_user, now)
            identity.binding_auth_version = int(target_user.auth_version)
            identity.last_login_at = now
            identity.link_failed_count = 0
            identity.link_first_failed_at = None
            identity.link_locked_until = None
            session_payload = issue_miniprogram_session(
                identity,
                target_user,
                now=now,
            )
            MiniProgramLinkChallenge.query.filter(
                MiniProgramLinkChallenge.user_id == target_user.id,
                MiniProgramLinkChallenge.id != challenge.id,
                MiniProgramLinkChallenge.consumed_at.is_(None),
                MiniProgramLinkChallenge.revoked_at.is_(None),
            ).update(
                {MiniProgramLinkChallenge.revoked_at: now},
                synchronize_session=False,
            )
            log_security_event(
                "miniprogram_account_linked",
                actor_id=target_user.id,
                actor_role=target_user.role,
                resource_type="miniprogram_identity",
                resource_id=str(identity.id),
                extra_data={
                    "source_was_temporary": source_was_temporary,
                },
            )
            linked_account = {
                "id": int(target_user.id),
                "username": str(target_user.username),
                "phone_verified": target_user.phone_verified_at is not None,
            }
            db.session.commit()
    except AccountLinkError:
        db.session.rollback()
        raise
    except IntegrityError as exc:
        db.session.rollback()
        logger.info(
            "小程序绑定目标唯一约束冲突: identity_id=%s",
            identity_id,
        )
        raise AccountLinkError(
            "target_already_linked",
            "该网页账号已经绑定其他微信，请联系管理员确认。",
            409,
        ) from exc
    except Exception as exc:
        db.session.rollback()
        logger.exception(
            "跨端账号绑定事务失败: identity_id=%s",
            identity_id,
        )
        raise AccountLinkError(
            "account_link_failed",
            "账号绑定暂时失败，请稍后重试。",
            503,
        ) from exc

    return {
        **session_payload,
        "linked": True,
        "linked_account": linked_account,
    }
