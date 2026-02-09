# -*- coding: utf-8 -*-
"""Smoke tests for admin routes.

These routes are easy to accidentally break due to DB dialect detection or
Flask-SQLAlchemy/SQLAlchemy version differences.
"""


def _login_as(client, user_id: int):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True


def test_admin_dashboard_renders(client, db_session):
    from core.db_models import User

    admin = User(username='admin_test', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.commit()

    _login_as(client, admin.id)
    resp = client.get('/admin')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '管理后台仪表板' in body


def test_admin_statistics_renders(client, db_session):
    from core.db_models import User

    admin = User(username='admin_stats', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.commit()

    _login_as(client, admin.id)
    resp = client.get('/admin/statistics')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '统计分析' in body

