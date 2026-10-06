"""验证真实找回授权、同意边界与凭证失效。"""
import pytest


def users(db_session):
    from core.db_models import User
    result = []
    for name in ('elder', 'relative', 'stranger'):
        user = User(username=name, role='caregiver')
        user.set_password('old-password')
        db_session.add(user)
        result.append(user)
    db_session.commit()
    return result


def login(client, user):
    from flask import g
    g.pop("_login_user", None)
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'csrf-test'


def post(client, **data):
    return client.post('/account/security', data={'csrf_token': 'csrf-test',
        'current_password': 'old-password', **data})


def test_recovery_requires_real_delegate_and_reauth(client, db_session):
    from core.db_models import RecoveryDelegate, AuditLog
    from services.account_service import can_reset
    elder, relative, stranger = users(db_session)
    login(client, relative)
    post(client, action='reset', target_username='elder', new_password='new-password', confirm_password='new-password')
    assert elder.check_password('old-password')
    login(client, elder)
    post(client, action='delegate', delegate_username='relative')
    assert RecoveryDelegate.query.count() == 1
    assert can_reset(relative, elder) and not can_reset(stranger, elder)
    login(client, relative)
    post(client, action='reset', target_username='elder', current_password='bad', new_password='new-password', confirm_password='new-password')
    assert elder.check_password('old-password')
    post(client, action='reset', target_username='elder', new_password='new-password', confirm_password='new-password')
    assert elder.check_password('new-password')
    logs = AuditLog.query.filter_by(action='password_reset').all()
    assert len(logs) == 1 and logs[0].extra_data is None


def test_reset_invalidates_sessions_and_api(client, app, db_session):
    from core.usage import create_api_token
    from services.account_service import reset_password
    elder, _, _ = users(db_session)
    login(client, elder)
    token = create_api_token(elder.id)
    reset_password(elder, elder, 'old-password', 'new-password')
    db_session.commit()
    assert client.get('/account/security').status_code == 302
    assert client.get('/mp/api/v1/me', headers={'Authorization': f'Bearer {token}'}).status_code == 401


def test_community_self_declared_location_is_not_acl(db_session):
    from services.account_service import can_reset
    elder, admin, _ = users(db_session)
    admin.role = 'community'
    admin.authorized_community = 'A'
    elder.community = 'A'
    assert not can_reset(admin, elder)
    elder.authorized_community = 'B'
    assert not can_reset(admin, elder)
    elder.authorized_community = 'A'
    assert can_reset(admin, elder)
    elder.role = 'admin'
    assert not can_reset(admin, elder)


def test_delegate_revoke(client, db_session):
    from services.account_service import can_reset
    elder, relative, _ = users(db_session)
    login(client, elder)
    post(client, action='delegate', delegate_username='relative')
    post(client, action='revoke_delegate', delegate_username='relative')
    assert not can_reset(relative, elder)


def test_security_csrf_and_guest_boundaries(client, db_session):
    elder, _, _ = users(db_session)
    login(client, elder)
    response = client.post('/account/security', data={'action': 'close', 'current_password': 'old-password', 'confirm_close': '注销'})
    assert response.status_code in (400, 403)
    assert elder.deleted_at is None


def test_consent_not_prechecked_and_password_required(client, db_session):
    from services.account_service import has_health_consent
    elder, _, _ = users(db_session)
    login(client, elder)
    response = client.get('/account/security')
    assert response.status_code == 200
    assert b'name="health_consent" required>' in response.data
    post(client, action='grant_consent', health_consent='on', current_password='bad')
    assert not has_health_consent(elder)
    post(client, action='grant_consent', health_consent='on')
    assert has_health_consent(elder)






def test_closure_scrubs_identity_and_disables_credentials(client, db_session):
    from core.usage import create_api_token
    elder, _, _ = users(db_session)
    elder.email = 'person@example.com'
    token = create_api_token(elder.id)
    login(client, elder)
    post(client, action='close', confirm_close='注销')
    assert elder.deleted_at and not elder.is_active
    assert elder.email is None and elder.username.startswith('deleted_mp_')
    assert client.get('/mp/api/v1/me', headers={'Authorization': f'Bearer {token}'}).status_code == 401


def test_forgot_password_reports_no_mail_channel(client, db_session):
    response = client.get('/forgot-password')
    assert response.status_code == 200
    assert '不会发送重置邮件' in response.text


def test_member_withdrawal_deletes_only_its_assessments_in_formal_web(app, client, db_session):
    """正式双端模式撤回指定成员，不清除本人或其他成员的历史评估。"""
    from core.db_models import FamilyMember, HealthRiskAssessment
    from services.account_service import grant_health_consent, has_health_consent
    owner, _, _ = users(db_session)
    selected = FamilyMember(user_id=owner.id, name='待撤回成员')
    other = FamilyMember(user_id=owner.id, name='其他成员')
    db_session.add_all([selected, other])
    db_session.flush()
    for subject in (owner, selected, other):
        grant_health_consent(subject, owner)
    own_assessment = HealthRiskAssessment(user_id=owner.id, recommendations='本人评估')
    selected_assessment = HealthRiskAssessment(user_id=owner.id, member_id=selected.id, recommendations='指定成员评估')
    other_assessment = HealthRiskAssessment(user_id=owner.id, member_id=other.id, recommendations='其他成员评估')
    db_session.add_all([own_assessment, selected_assessment, other_assessment])
    db_session.commit()
    selected_id = selected.id
    kept_ids = {own_assessment.id, other_assessment.id}
    app.config.update(WECHAT_FORMAL_RUNTIME=True, WEB_PRIVATE_FEATURES_ENABLED=True)
    login(client, owner)
    response = post(client, action='withdraw_member', member_id=selected_id)
    assert response.status_code == 302
    assert not has_health_consent(selected)
    assert has_health_consent(owner) and has_health_consent(other)
    assert HealthRiskAssessment.query.filter_by(member_id=selected_id).count() == 0
    assert {row.id for row in HealthRiskAssessment.query.all()} == kept_ids
    # 重新同意也不能恢复已撤回并删除的敏感评估。
    grant_health_consent(selected, owner)
    db_session.commit()
    assert HealthRiskAssessment.query.filter_by(member_id=selected_id).count() == 0
