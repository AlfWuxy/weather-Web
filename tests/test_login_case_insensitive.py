# -*- coding: utf-8 -*-
"""登录/注册用户名大小写必须与锁定桶一致。"""


def _csrf(client, token='csrf-case'):
    with client.session_transaction() as session:
        session['_csrf_token'] = token
    return token


def test_login_finds_user_when_username_case_differs(client, db_session):
    from core.db_models import User

    user = User(username='caseuser', role='user')
    user.set_password('correct-password')
    db_session.add(user)
    db_session.commit()

    csrf = _csrf(client)
    response = client.post(
        '/login',
        data={
            'username': 'CaseUser',
            'password': 'correct-password',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert response.headers['Location'].endswith('/dashboard')


def test_register_rejects_username_that_only_differs_by_case(client, db_session, app):
    from core.db_models import User
    from core.extensions import limiter

    existing = User(username='caseuser', role='user')
    existing.set_password('correct-password')
    db_session.add(existing)
    db_session.commit()

    limiter.reset()
    csrf = _csrf(client, 'csrf-register-case')
    response = client.post(
        '/register',
        data={
            'username': 'CaseUser',
            'password': 'pass1234',
            'age': '40',
            'gender': '男',
            'csrf_token': csrf,
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert '用户名已存在' in response.get_data(as_text=True)
    assert User.query.filter(User.username.in_(['CaseUser', 'caseuser'])).count() == 1


def test_caregiver_login_next_pairs_lands_on_workbench(client, db_session):
    from core.db_models import User

    user = User(username='care-next', role='caregiver', community='朝阳社区')
    user.set_password('testpass')
    db_session.add(user)
    db_session.commit()

    csrf = _csrf(client, 'csrf-care-next')
    response = client.post(
        '/login',
        query_string={'next': '/pairs'},
        data={
            'username': 'care-next',
            'password': 'testpass',
            'csrf_token': csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert response.headers['Location'].endswith('/caregiver')
