"""邮件恢复默认关闭，测试仅使用内存发送器。"""
from datetime import timedelta
import re
import pytest
from tests.test_account_privacy import users, login, post


@pytest.fixture
def mail(app):
    sent = []
    app.config.update(ACCOUNT_MAIL_ENABLED=True, PUBLIC_BASE_URL='https://weather.example',
        ACCOUNT_MAIL_TEST_SENDER=lambda recipient, subject, body: sent.append((recipient, subject, body)))
    return sent


def last_token(mail):
    return re.search(r'#([A-Za-z0-9_-]+)', mail[-1][2]).group(1)


def test_mail_disabled_no_false_send(app, client, db_session):
    from services.account_email_service import mail_available, issue_email
    elder, _, _ = users(db_session)
    elder.email = 'elder@example.com'
    app.config['ACCOUNT_MAIL_ENABLED'] = False
    assert not mail_available() and not issue_email(elder, 'verify')
    assert '不会发送重置邮件' in client.get('/forgot-password').text


def test_verify_then_recover_single_use(mail, client, db_session):
    from core.db_models import AccountEmailToken
    from core.usage import create_api_token, verify_api_token
    from services.account_email_service import issue_email, consume_email
    elder, _, _ = users(db_session)
    elder.email = 'elder@example.com'
    db_session.commit()
    assert not issue_email(elder, 'reset')
    assert issue_email(elder, 'verify')
    token = last_token(mail)
    row = AccountEmailToken.query.one()
    assert row.token_hash != token and len(row.token_hash) == 64
    consume_email(token, 'verify')
    assert elder.email_verified_at
    with pytest.raises(ValueError):
        consume_email(token, 'verify')
    api = create_api_token(elder.id)
    login(client, elder)
    assert issue_email(elder, 'reset')
    reset = last_token(mail)
    consume_email(reset, 'reset', 'new-password')
    assert elder.check_password('new-password')
    assert verify_api_token(api) is None
    assert client.get('/account/security').status_code == 302
    with pytest.raises(ValueError):
        consume_email(reset, 'reset', 'another-password')


@pytest.mark.parametrize('change', ['expire', 'email', 'password', 'close'])
def test_token_bound_to_lifetime_email_password_and_account(mail, db_session, change):
    from core.db_models import AccountEmailToken
    from core.time_utils import utcnow
    from services.account_email_service import issue_email, consume_email
    elder, _, _ = users(db_session)
    elder.email = 'elder@example.com'
    db_session.commit()
    issue_email(elder, 'verify')
    token = last_token(mail)
    if change == 'expire':
        AccountEmailToken.query.one().expires_at = utcnow() - timedelta(seconds=1)
    elif change == 'email':
        elder.email = 'changed@example.com'
    elif change == 'password':
        elder.set_password('changed-password')
    else:
        elder.deleted_at = utcnow()
    db_session.commit()
    with pytest.raises(ValueError):
        consume_email(token, 'verify')


def test_no_enumeration_and_failure_revokes_token(mail, client, app, db_session):
    from core.db_models import AccountEmailToken
    from core.time_utils import utcnow
    elder, _, _ = users(db_session)
    elder.email = 'elder@example.com'
    elder.email_verified_at = utcnow()
    db_session.commit()
    with client.session_transaction() as session:
        session['_csrf_token'] = 'csrf-test'
    response_a = client.post('/forgot-password', data={'email': elder.email, 'csrf_token': 'csrf-test'})
    response_b = client.post('/forgot-password', data={'email': 'missing@example.com', 'csrf_token': 'csrf-test'})
    assert response_a.status_code == response_b.status_code == 200
    assert '如果该邮箱关联有效且已验证的账号' in response_a.text
    assert '如果该邮箱关联有效且已验证的账号' in response_b.text
    def broken(*args):
        raise RuntimeError('should never be logged')
    app.config['ACCOUNT_MAIL_TEST_SENDER'] = broken
    response_c = client.post('/forgot-password', data={'email': elder.email, 'csrf_token': 'csrf-test'})
    assert response_c.status_code == 200
    assert AccountEmailToken.query.filter_by(used_at=None).count() == 0


def test_verify_route_requires_password_and_csrf(mail, client, db_session):
    elder, _, _ = users(db_session)
    elder.email = 'elder@example.com'
    db_session.commit()
    login(client, elder)
    post(client, action='verify_email', current_password='wrong')
    assert not mail
    post(client, action='verify_email')
    assert len(mail) == 1
    token = last_token(mail)
    # GET不会消费，邮件预览爬虫不能确认邮箱。
    assert client.get('/account/email-token?purpose=verify').status_code == 200
    assert elder.email_verified_at is None
    assert client.post('/account/email-token', data={'purpose': 'verify', 'token': token}).status_code in (400, 403)
    response = client.post('/account/email-token', data={'purpose': 'verify', 'token': token, 'csrf_token': 'csrf-test'})
    assert response.status_code == 302 and elder.email_verified_at


def test_forged_host_never_used_for_email_links(mail, client, db_session):
    from services.account_email_service import issue_email
    elder, _, _ = users(db_session)
    elder.email = 'elder@example.com'
    db_session.commit()
    issue_email(elder, 'verify')
    assert 'https://weather.example/account/email-token?purpose=verify#' in mail[0][2]


def test_mail_rate_limit(mail, client, app, db_session):
    from core.extensions import limiter
    limiter.reset()
    with client.session_transaction() as session:
        session['_csrf_token'] = 'csrf-test'
    statuses = [client.post('/forgot-password', data={'email': 'missing@example.com', 'csrf_token': 'csrf-test'}).status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5 and statuses[-1] == 429
    limiter.reset()


def test_admin_reset_requires_reauth_and_revokes_api(client, db_session):
    from core.usage import create_api_token, verify_api_token
    elder, admin, _ = users(db_session)
    admin.role = 'admin'
    db_session.commit()
    token = create_api_token(elder.id)
    login(client, admin)
    data = {'username': elder.username, 'role': 'user', 'password': 'new-password', 'csrf_token': 'csrf-test'}
    client.post(f'/admin/user/{elder.id}/edit', data=data)
    assert elder.check_password('old-password') and verify_api_token(token)
    client.post(f'/admin/user/{elder.id}/edit', data={**data, 'current_password': 'old-password'})
    assert elder.check_password('new-password') and verify_api_token(token) is None
