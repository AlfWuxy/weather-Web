"""新找回入口沿用生产认证版本与跨端撤销语义。"""
from datetime import timedelta
from core.time_utils import utcnow
from core.db_models import (User, FamilyMember, FamilyMemberProfile, MiniProgramIdentity,
    MiniProgramSession, MiniProgramLinkChallenge, AccountEmailToken, RecoveryDelegate)
from services.account_service import (reset_password, grant_health_consent, has_health_consent,
    withdraw_health, prepare_account_deletion)
from tests.test_account_privacy import users


def test_reset_revokes_all_production_credentials(db_session):
    elder, _, _ = users(db_session)
    now = utcnow()
    identity = MiniProgramIdentity(user_id=elder.id, openid_hash='a'*64,
        privacy_consent_version='v1', privacy_consented_at=now, binding_auth_version=elder.auth_version)
    db_session.add(identity); db_session.flush()
    session = MiniProgramSession(identity_id=identity.id, user_id=elder.id, token_hash='b'*64,
        privacy_consent_version='v1', expires_at=now+timedelta(hours=1))
    db_session.add(session)
    token = AccountEmailToken(user_id=elder.id, purpose='reset', token_hash='c'*64,
        email_hash='d'*64, password_stamp='e'*64, expires_at=now+timedelta(hours=1))
    db_session.add(token);db_session.commit()
    version = elder.auth_version
    reset_password(elder, elder, 'old-password', 'new-password')
    db_session.commit();db_session.expire_all()
    assert elder.auth_version == version+1
    assert identity.binding_auth_version != elder.auth_version
    assert session.revoked_at and token.used_at


def test_shared_withdrawal_clears_both_subject_receipts(db_session):
    elder, _, _ = users(db_session)
    member = FamilyMember(user_id=elder.id, name='sample', chronic_diseases='[]')
    db_session.add(member);db_session.flush()
    grant_health_consent(elder, elder);grant_health_consent(member, elder)
    db_session.add(FamilyMemberProfile(member_id=member.id, medications='sample'))
    db_session.commit()
    assert has_health_consent(elder) and has_health_consent(member)
    withdraw_health(elder);db_session.commit()
    assert not has_health_consent(elder) and not has_health_consent(member)
    assert FamilyMemberProfile.query.count() == 0


def test_predelete_removes_new_recovery_metadata(db_session):
    elder, relative, _ = users(db_session)
    db_session.add(RecoveryDelegate(user_id=elder.id, delegate_id=relative.id, verified_at=utcnow()))
    elder.email_verified_at = utcnow();db_session.commit()
    prepare_account_deletion(elder);db_session.commit()
    assert RecoveryDelegate.query.count() == 0
    assert elder.email_verified_at is None
