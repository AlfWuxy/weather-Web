# -*- coding: utf-8 -*-
"""管理员角色变更必须保留至少一个管理员。"""
import threading
import time


def _login_as(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'last-admin-csrf'


def test_sole_admin_cannot_demote_self(client, db_session):
    from core.db_models import User

    admin = User(username='sole_admin', role='admin')
    admin.set_password('AdminPassword1!')
    db_session.add(admin)
    db_session.commit()
    admin_id = int(admin.id)
    _login_as(client, admin)

    response = client.post(
        f'/admin/user/{admin_id}/edit',
        data={
            'username': admin.username,
            'email': '',
            'age': '',
            'gender': '',
            'community': '',
            'authorized_community': '',
            'role': 'user',
            'password': '',
            'csrf_token': 'last-admin-csrf',
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert response.headers['Location'].endswith(f'/admin/user/{admin_id}/edit')
    db_session.expire_all()
    assert db_session.get(User, admin_id).role == 'admin'


def test_concurrent_admin_demotions_are_serialized(app, db_session):
    from core.db_models import User
    from core.extensions import db
    from services.user.admin_role_guard import (
        LastAdminError,
        serialized_admin_role_change,
    )

    admins = [
        User(username='parallel_admin_one', role='admin'),
        User(username='parallel_admin_two', role='admin'),
    ]
    for admin in admins:
        admin.set_password('AdminPassword1!')
    db_session.add_all(admins)
    db_session.commit()
    admin_ids = [int(admin.id) for admin in admins]

    start = threading.Barrier(2)
    outcomes = []
    failures = []
    result_lock = threading.Lock()

    def _demote(user_id):
        with app.app_context():
            try:
                start.wait(timeout=3)
                with serialized_admin_role_change(user_id, 'user') as target:
                    target.role = 'user'
                    # 扩大两个事务重叠窗口，验证第二个事务会等待并重新计数。
                    time.sleep(0.1)
                    db.session.commit()
                result = 'demoted'
            except LastAdminError:
                result = 'blocked'
            except Exception as exc:  # pragma: no cover - 失败信息由主线程断言展示
                with result_lock:
                    failures.append(repr(exc))
                return
            finally:
                db.session.remove()
            with result_lock:
                outcomes.append(result)

    threads = [threading.Thread(target=_demote, args=(user_id,)) for user_id in admin_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert sorted(outcomes) == ['blocked', 'demoted']
    db_session.expire_all()
    assert User.query.filter_by(role='admin').count() == 1
