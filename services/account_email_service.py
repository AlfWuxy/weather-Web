# -*- coding: utf-8 -*-
"""可选安全邮件通道；默认关闭，未配置时不声称发送。"""
import hashlib
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import urlsplit
from flask import current_app
from core.db_models import AccountEmailToken, AuditLog, User
from core.extensions import db
from core.time_utils import utcnow, ensure_utc_aware
from services.account_service import revoke_tokens
from utils.validators import validate_password


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _base_url():
    base = (current_app.config.get('PUBLIC_BASE_URL') or '').rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return base


def mail_available():
    config = current_app.config
    if not config.get('ACCOUNT_MAIL_ENABLED') or not _base_url():
        return False
    if config.get('TESTING') and callable(config.get('ACCOUNT_MAIL_TEST_SENDER')):
        return True
    return all(config.get(key) for key in ('ACCOUNT_SMTP_HOST', 'ACCOUNT_SMTP_PORT', 'ACCOUNT_SMTP_USERNAME', 'ACCOUNT_SMTP_PASSWORD', 'ACCOUNT_MAIL_FROM'))


def _send(recipient, purpose, token):
    # 令牌放片段，避免写入代理访问日志或 Referer；页面再转入受 CSRF 保护的 POST。
    url = f"{_base_url()}/account/email-token?purpose={purpose}#{token}"
    subject = '确认找回邮箱' if purpose == 'verify' else '重置账号密码'
    body = f'{subject}：请打开以下链接，在页面确认。链接15分钟内有效，只能使用一次。\n{url}\n如果您未发起该请求，请忽略。'
    config = current_app.config
    sender = config.get('ACCOUNT_MAIL_TEST_SENDER')
    if config.get('TESTING') and callable(sender):
        sender(recipient, subject, body)
        return
    message = EmailMessage()
    message['From'] = config['ACCOUNT_MAIL_FROM']
    message['To'] = recipient
    message['Subject'] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    if config.get('ACCOUNT_SMTP_SSL'):
        smtp = smtplib.SMTP_SSL(config['ACCOUNT_SMTP_HOST'], int(config['ACCOUNT_SMTP_PORT']), timeout=10, context=context)
    else:
        smtp = smtplib.SMTP(config['ACCOUNT_SMTP_HOST'], int(config['ACCOUNT_SMTP_PORT']), timeout=10)
    with smtp:
        if not config.get('ACCOUNT_SMTP_SSL'):
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(config['ACCOUNT_SMTP_USERNAME'], config['ACCOUNT_SMTP_PASSWORD'])
        smtp.send_message(message)


def _issue_email_locked(user, purpose):
    if purpose not in ('verify', 'reset') or not mail_available() or not user or not user.is_active or not user.email:
        return False
    if purpose == 'reset' and not user.email_verified_at:
        return False
    token = secrets.token_urlsafe(32)
    now = utcnow()
    AccountEmailToken.query.filter_by(user_id=user.id, purpose=purpose, used_at=None).update({'used_at': now})
    row = AccountEmailToken(user_id=user.id, purpose=purpose,
        token_hash=_digest(token), email_hash=_digest(user.email.lower()),
        password_stamp=_digest(user.password_hash), expires_at=now + timedelta(minutes=15))
    db.session.add(row)
    db.session.commit()
    try:
        _send(user.email, purpose, token)
    except Exception:
        # SMTP 错误可能含地址/凭证，不把异常原文或令牌写入日志。
        row.used_at = utcnow()
        db.session.commit()
        current_app.logger.warning('账号邮件发送失败，令牌已撤销')
        return False
    return True


def _consume_email_locked(token, purpose, password=None):
    if not isinstance(token, str) or len(token) < 32 or len(token) > 128 or purpose not in ('verify', 'reset'):
        raise ValueError('链接无效或已过期')
    row = AccountEmailToken.query.filter_by(token_hash=_digest(token), purpose=purpose).first()
    user = db.session.get(User, row.user_id) if row else None
    now = utcnow()
    if (not row or row.used_at or ensure_utc_aware(row.expires_at) <= now or not user or not user.is_active
            or not user.email or row.email_hash != _digest(user.email.lower())
            or row.password_stamp != _digest(user.password_hash)
            or (purpose == 'reset' and not user.email_verified_at)):
        raise ValueError('链接无效或已过期')
    if purpose == 'reset':
        valid, result = validate_password(password)
        if not valid:
            raise ValueError(result)
    # 条件更新保证并发重放中只有一个事务能消费；改密与消费同事务。
    changed = AccountEmailToken.query.filter(AccountEmailToken.id == row.id,
        AccountEmailToken.used_at.is_(None), AccountEmailToken.expires_at > now).update({'used_at': now}, synchronize_session=False)
    if changed != 1:
        db.session.rollback()
        raise ValueError('链接无效或已过期')
    if purpose == 'verify':
        user.email_verified_at = now
    else:
        user.set_password(result)
        revoke_tokens(user)
        AccountEmailToken.query.filter_by(user_id=user.id, used_at=None).update({'used_at': now})
    db.session.add(AuditLog(actor_role='email_token', action='email_verified' if purpose == 'verify' else 'password_reset_email', resource_type='user', resource_id=str(user.id)))
    db.session.commit()
    return user


def issue_email(user, purpose, *, locked=False):
    if not user or not mail_available():
        return False
    if locked:
        return _issue_email_locked(user, purpose)
    from services.account_service import account_write_guard
    from services.user.owner_write_guard import OwnerInactiveError
    uid = int(user.id)
    try:
        with account_write_guard(uid) as users:
            return _issue_email_locked(users[uid], purpose)
    except OwnerInactiveError:
        return False


def consume_email(token, purpose, password=None):
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise ValueError('链接无效或已过期')
    row = AccountEmailToken.query.filter_by(token_hash=_digest(token), purpose=purpose).first()
    if row is None:
        raise ValueError('链接无效或已过期')
    uid = int(row.user_id)
    from services.account_service import account_write_guard
    from services.user.owner_write_guard import OwnerInactiveError
    try:
        with account_write_guard(uid):
            return _consume_email_locked(token, purpose, password)
    except OwnerInactiveError as exc:
        raise ValueError('链接无效或已过期') from exc
