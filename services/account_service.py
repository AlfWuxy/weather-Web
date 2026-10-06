# -*- coding: utf-8 -*-
"""账号恢复、独立健康同意与撤回；写入由调用方在 owner 锁内提交。"""
from contextlib import contextmanager, ExitStack
from core.extensions import db
from core.time_utils import utcnow
from core.db_models import (User, FamilyMember, FamilyMemberProfile, HealthDiary,
    MedicationReminder, HealthRiskAssessment, ApiToken, RecoveryDelegate, AuditLog,
    Pair, PairLink, PairActionToken, Notification, AccountEmailToken,
    MiniProgramSession, MiniProgramLinkChallenge, Debrief, DailyStatus, UsageEvent)
from services.miniprogram_auth import current_privacy_version
from services.user.owner_write_guard import OwnerInactiveError, _lock_active_owner_for_write
from services.push.locks import push_owner_lock
from utils.validators import validate_password


def has_health_consent(subject):
    return bool(subject and getattr(subject, 'health_sensitive_consented_at', None)
        and getattr(subject, 'health_sensitive_consent_version', None) == current_privacy_version())


@contextmanager
def account_owner_locks(*user_ids):
    # 与注销和跨端绑定保持相同的文件锁顺序。
    db.session.rollback()
    with ExitStack() as stack:
        for uid in sorted({int(value) for value in user_ids}):
            stack.enter_context(push_owner_lock(uid))
        yield


@contextmanager
def account_write_guard(*user_ids):
    # 多账号操作先按编号取得所有文件锁，再以同序取得数据库锁。
    ids = sorted({int(value) for value in user_ids})
    db.session.rollback()
    with ExitStack() as stack:
        for uid in ids:
            stack.enter_context(push_owner_lock(uid))
        users = {uid: _lock_active_owner_for_write(uid) for uid in ids}
        if any(user is None for user in users.values()):
            db.session.rollback()
            raise OwnerInactiveError('账号已停用')
        try:
            yield users
        finally:
            db.session.rollback()


def audit(actor, action, target):
    if target.id is None:
        db.session.add(target)
        db.session.flush()
    db.session.add(AuditLog(actor_id=actor.id, actor_role=actor.role, action=action,
        resource_type='family_member' if isinstance(target, FamilyMember) else 'user', resource_id=str(target.id)))


def grant_health_consent(subject, actor):
    subject.health_sensitive_consented_at = utcnow()
    subject.health_sensitive_consent_version = current_privacy_version()
    audit(actor, 'health_consent_granted', subject)


def revoke_tokens(user):
    # 与生产 Web / 微信身份版本一致；调用方不能再额外递增版本。
    user.auth_version = int(user.auth_version or 1) + 1
    now = utcnow()
    for model in (ApiToken, MiniProgramSession, MiniProgramLinkChallenge):
        model.query.filter_by(user_id=user.id, revoked_at=None).update({'revoked_at': now}, synchronize_session=False)
    AccountEmailToken.query.filter_by(user_id=user.id, used_at=None).update({'used_at': now}, synchronize_session=False)
    pairs = [row.id for row in Pair.query.filter_by(caregiver_id=user.id)]
    if pairs:
        PairActionToken.query.filter(PairActionToken.pair_id.in_(pairs)).update({'revoked_at': now}, synchronize_session=False)
    PairLink.query.filter_by(caregiver_id=user.id).update({'status': 'expired', 'expires_at': now}, synchronize_session=False)
    Pair.query.filter_by(caregiver_id=user.id).update({'short_code_expires_at': now}, synchronize_session=False)


def can_reset(actor, target):
    if not actor or not target or not actor.is_active or not target.is_active:
        return False
    if actor.id == target.id:
        return True
    if actor.role == 'community':
        return bool(actor.authorized_community and target.role in ('user', 'caregiver')
            and target.authorized_community == actor.authorized_community)
    if actor.role == 'admin':
        return target.role in ('user', 'caregiver')
    return bool(target.role in ('user', 'caregiver') and RecoveryDelegate.query.filter_by(
        user_id=target.id, delegate_id=actor.id, revoked_at=None).first())


def reset_password(actor, target, actor_password, password):
    if not actor.check_password(actor_password) or not can_reset(actor, target):
        raise ValueError('无法验证操作者或找回授权')
    valid, value = validate_password(password)
    if not valid:
        raise ValueError(value)
    target.set_password(value)
    revoke_tokens(target)
    audit(actor, 'password_reset', target)


def _clear_pair_health_context(pair_ids):
    if not pair_ids:
        return
    DailyStatus.query.filter(DailyStatus.pair_id.in_(pair_ids)).update(
        {'caregiver_note': None, 'caregiver_actions': None, 'elder_actions': None, 'risk_level': '未知'}, synchronize_session=False)
    Debrief.query.filter(db.or_(Debrief.pair_id.in_(pair_ids), Debrief.origin_pair_id.in_(pair_ids))).delete(synchronize_session=False)
    UsageEvent.query.filter(UsageEvent.pair_id.in_(pair_ids)).update({'meta_json': None}, synchronize_session=False)


def withdraw_member(member, actor):
    _clear_pair_health_context([row.id for row in Pair.query.filter_by(member_id=member.id)])
    Notification.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    member.health_sensitive_consented_at = None
    member.health_sensitive_consent_version = None
    member.chronic_diseases = None
    FamilyMemberProfile.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    HealthDiary.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    MedicationReminder.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    HealthRiskAssessment.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    audit(actor, 'member_health_consent_withdrawn', member)


def withdraw_health(user):
    _clear_pair_health_context([row.id for row in Pair.query.filter_by(caregiver_id=user.id)])
    Debrief.query.filter_by(owner_user_id=user.id).delete(synchronize_session=False)
    user.health_sensitive_consented_at = None
    user.health_sensitive_consent_version = None
    user.chronic_diseases = None
    user.has_chronic_disease = None
    for member in FamilyMember.query.filter_by(user_id=user.id):
        withdraw_member(member, user)
    for model in (HealthDiary, MedicationReminder, HealthRiskAssessment, Notification):
        model.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    UsageEvent.query.filter_by(user_id=user.id).update({'meta_json': None}, synchronize_session=False)
    audit(user, 'health_consent_withdrawn', user)


def prepare_account_deletion(user):
    # 此入口由生产统一注销流程调用，保留其墓碑、跨端身份和审计去关联语义。
    revoke_tokens(user)
    RecoveryDelegate.query.filter(db.or_(RecoveryDelegate.user_id == user.id,
        RecoveryDelegate.delegate_id == user.id)).delete(synchronize_session=False)
    AccountEmailToken.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    user.email_verified_at = None
    user.authorized_community = None
    # 新产品表引用 pair；先删除回执，避免旧注销流程被外键阻断。
    from core.db_models import CoolingFeedback
    pair_ids = [row.id for row in Pair.query.filter_by(caregiver_id=user.id)]
    if pair_ids:
        CoolingFeedback.query.filter(CoolingFeedback.pair_id.in_(pair_ids)).delete(synchronize_session=False)


def close_account(user):
    if user.role in ('admin', 'community'):
        raise ValueError('管理账号请先由其他管理员解除管理职责，再注销')
    from blueprints.mp_api import _anonymize_miniprogram_owner
    # prepare_account_deletion 在统一清理入口执行，Web / 小程序均不会漏掉新表。
    return _anonymize_miniprogram_owner(user)
