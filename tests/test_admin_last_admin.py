# -*- coding: utf-8 -*-
"""P9：最后一个 admin 不可降级；多 admin 可降；角色/改密写审计。"""
from core.db_models import User


def _csrf(client, token='p9-admin-csrf'):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = token
    return token


def _login_admin(client, db_session, username='p9admin', password='testpass99'):
    admin = User(username=username, role='admin')
    admin.set_password(password)
    db_session.add(admin)
    db_session.commit()
    csrf = _csrf(client)
    client.post(
        '/login',
        data={'username': username, 'password': password, 'csrf_token': csrf},
        follow_redirects=True,
    )
    return admin, csrf


def _edit_payload(user, csrf, **overrides):
    data = {
        'username': user.username,
        'email': user.email or '',
        'age': str(user.age or ''),
        'gender': user.gender or '',
        'community': user.community or '',
        'role': user.role or 'user',
        'password': '',
        'csrf_token': csrf,
    }
    data.update(overrides)
    return data


def test_cannot_demote_sole_admin(client, db_session):
    """唯一 admin 不可被降为 user。"""
    admin, csrf = _login_admin(client, db_session, username='sole_admin')
    resp = client.post(
        f'/admin/user/{admin.id}/edit',
        data=_edit_payload(admin, csrf, role='user'),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db_session.refresh(admin)
    assert admin.role == 'admin'
    body = resp.get_data(as_text=True)
    assert '最后一个管理员' in body or '不能降级' in body


def test_can_demote_when_another_admin_exists(client, db_session):
    """存在第二 admin 时，可降级目标 admin。"""
    admin, csrf = _login_admin(client, db_session, username='admin_a')
    other = User(username='admin_b', role='admin')
    other.set_password('testpass99')
    db_session.add(other)
    db_session.commit()

    resp = client.post(
        f'/admin/user/{other.id}/edit',
        data=_edit_payload(other, csrf, role='user'),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db_session.refresh(other)
    assert other.role == 'user'
    # 操作者仍为 admin
    db_session.refresh(admin)
    assert admin.role == 'admin'


def test_cannot_delete_admin_account(client, db_session):
    """删除路径仍拒绝 admin。"""
    admin, csrf = _login_admin(client, db_session, username='admin_del')
    other = User(username='admin_keep', role='admin')
    other.set_password('testpass99')
    db_session.add(other)
    db_session.commit()

    resp = client.post(
        f'/admin/user/{other.id}/delete',
        data={'csrf_token': csrf},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert User.query.filter_by(id=other.id).first() is not None
    assert User.query.filter_by(id=other.id).first().role == 'admin'


def test_can_promote_user_to_admin(client, db_session):
    """普通用户可提升为 admin（不破坏提权路径）。"""
    admin, csrf = _login_admin(client, db_session, username='admin_promo')
    target = User(username='plain_user', role='user')
    target.set_password('testpass99')
    db_session.add(target)
    db_session.commit()

    resp = client.post(
        f'/admin/user/{target.id}/edit',
        data=_edit_payload(target, csrf, role='admin'),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db_session.refresh(target)
    assert target.role == 'admin'


def test_non_admin_cannot_edit_users(client, db_session):
    """非 admin 不可改用户角色。"""
    user = User(username='normal_p9', role='user')
    user.set_password('testpass99')
    victim = User(username='victim_p9', role='user')
    victim.set_password('testpass99')
    db_session.add_all([user, victim])
    db_session.commit()

    csrf = _csrf(client)
    client.post(
        '/login',
        data={'username': 'normal_p9', 'password': 'testpass99', 'csrf_token': csrf},
        follow_redirects=True,
    )
    client.post(
        f'/admin/user/{victim.id}/edit',
        data=_edit_payload(victim, csrf, role='admin'),
        follow_redirects=True,
    )
    db_session.refresh(victim)
    assert victim.role == 'user'


def test_helpers_last_admin_logic(app, db_session):
    """单元：_would_demote_last_admin 边界。"""
    from blueprints.admin import _would_demote_last_admin, _count_admin_users

    with app.app_context():
        a = User(username='h1', role='admin')
        a.set_password('x')
        db_session.add(a)
        db_session.commit()
        assert _count_admin_users() >= 1
        assert _would_demote_last_admin(a, 'user') is True
        assert _would_demote_last_admin(a, 'admin') is False

        b = User(username='h2', role='admin')
        b.set_password('x')
        db_session.add(b)
        db_session.commit()
        assert _would_demote_last_admin(a, 'user') is False
        assert _would_demote_last_admin(b, 'caregiver') is False


def test_sole_admin_cannot_demote_self(client, db_session):
    """边界1：admin 降自己且是唯一 admin → 被拦（角色仍为 admin）。"""
    admin, csrf = _login_admin(client, db_session, username='sole_self')
    assert User.query.filter_by(role='admin').count() == 1

    resp = client.post(
        f'/admin/user/{admin.id}/edit',
        data=_edit_payload(admin, csrf, role='user'),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db_session.refresh(admin)
    assert admin.role == 'admin'
    assert User.query.filter_by(role='admin').count() == 1
    body = resp.get_data(as_text=True)
    assert '最后一个管理员' in body or '不能降级' in body


def test_admin_a_can_demote_admin_b_to_caregiver(client, db_session):
    """边界2：两个 admin，A 把 B 降为 caregiver → 成功。"""
    admin_a, csrf = _login_admin(client, db_session, username='p9_admin_a')
    admin_b = User(username='p9_admin_b', role='admin')
    admin_b.set_password('testpass99')
    db_session.add(admin_b)
    db_session.commit()
    assert User.query.filter_by(role='admin').count() >= 2

    resp = client.post(
        f'/admin/user/{admin_b.id}/edit',
        data=_edit_payload(admin_b, csrf, role='caregiver'),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db_session.refresh(admin_b)
    assert admin_b.role == 'caregiver'
    db_session.refresh(admin_a)
    assert admin_a.role == 'admin'


def test_admin_edit_password_requires_min_8_chars(client, db_session):
    """边界3（可选）：改密走 validate_password，少于 8 位被拒且原密码不变。"""
    admin, csrf = _login_admin(client, db_session, username='p9_pw_admin')
    target = User(username='p9_pw_target', role='user')
    target.set_password('original99')
    db_session.add(target)
    db_session.commit()
    target_id = target.id

    resp = client.post(
        f'/admin/user/{target_id}/edit',
        data=_edit_payload(target, csrf, password='short7'),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db_session.expire_all()
    target = User.query.get(target_id)
    assert target is not None
    assert target.check_password('original99')
    assert not target.check_password('short7')
    body = resp.get_data(as_text=True)
    assert '8' in body or '密码' in body


def test_admin_password_reset_audits_any_user(client, db_session, app):
    """I04：admin 重置任意用户密码 → 记 admin_password_reset（非空密码）。"""
    from core.db_models import AuditLog

    app.config['FEATURE_AUDIT_LOGS'] = True
    admin, csrf = _login_admin(client, db_session, username='p9_pw_audit_admin')
    target = User(username='p9_pw_audit_user', role='user')
    target.set_password('original99')
    db_session.add(target)
    db_session.commit()
    target_id = target.id
    admin_id = admin.id

    before = AuditLog.query.filter_by(action='admin_password_reset').count()
    resp = client.post(
        f'/admin/user/{target_id}/edit',
        data=_edit_payload(target, csrf, password='NewPass88!'),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db_session.expire_all()
    target = User.query.get(target_id)
    assert target is not None
    assert target.check_password('NewPass88!')

    rows = AuditLog.query.filter_by(action='admin_password_reset').all()
    # log_security_event + log_audit 各落一条，至少 +1（通常 +2）
    assert len(rows) >= before + 1
    matched = [r for r in rows if str(r.resource_id) == str(target_id)]
    assert matched, 'expected admin_password_reset for target user'
    row = matched[-1]
    assert row.actor_id == admin_id
    assert row.resource_type == 'user'
    # 审计不得落明文密码
    for r in matched:
        extra = r.extra_data or ''
        assert 'NewPass88!' not in extra
        assert 'original99' not in extra


def test_admin_empty_password_skips_password_audit(client, db_session, app):
    """I04：空密码不改密、不记 admin_password_reset。"""
    from core.db_models import AuditLog

    app.config['FEATURE_AUDIT_LOGS'] = True
    admin, csrf = _login_admin(client, db_session, username='p9_pw_empty_admin')
    target = User(username='p9_pw_empty_user', role='user')
    target.set_password('original99')
    db_session.add(target)
    db_session.commit()
    target_id = target.id

    before = AuditLog.query.filter_by(action='admin_password_reset').count()
    resp = client.post(
        f'/admin/user/{target_id}/edit',
        data=_edit_payload(target, csrf, password=''),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db_session.expire_all()
    target = User.query.get(target_id)
    assert target is not None
    assert target.check_password('original99')
    after = AuditLog.query.filter_by(action='admin_password_reset').count()
    assert after == before
