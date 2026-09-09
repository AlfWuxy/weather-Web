# -*- coding: utf-8 -*-
"""医生科普与短提醒：仅医生可起草发布，草稿不归因，禁词与有效期。"""
from datetime import timedelta

from core.db_models import AdviceContent, User
from core.extensions import db
from core.time_utils import utcnow
from services.advice_content_service import (
    AdviceContentError,
    active_templates,
    create_draft,
    publish_advice,
    serialize_advice,
    withdraw_advice,
)


SAFE_BODY = '高温天气注意补水，减少午后长时间户外活动。'
SAFE_SOURCE = '中国疾病预防控制中心'


def _user(username, role='user'):
    user = User(username=username, role=role)
    user.set_password('pass12344')
    db.session.add(user)
    db.session.commit()
    return user


def _csrf(client, token='advice-csrf'):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = token
    return token


def _login(client, username, token):
    return client.post(
        '/login',
        data={'username': username, 'password': 'pass12344', 'csrf_token': token},
        follow_redirects=True,
    )


def _logout(client, token):
    client.post('/logout', data={'csrf_token': token}, follow_redirects=False)


def _draft(user, **overrides):
    payload = {
        'kind': 'template',
        'title': '高温补水提醒',
        'body': SAFE_BODY,
        'source': SAFE_SOURCE,
        'scenario': 'heat',
    }
    payload.update(overrides)
    return create_draft(user, **payload)


def _assert_forbidden(fn):
    try:
        fn()
        assert False, 'should be forbidden'
    except AdviceContentError as exc:
        assert exc.code == 'forbidden'
        assert exc.status_code == 403


def _public_ids(rows):
    return [row.public_id for row in rows]


def _stub_doctor_templates(app):
    from jinja2 import ChoiceLoader, DictLoader

    app.jinja_env.loader = ChoiceLoader([
        DictLoader({
            'doctor_content_list.html': 'doctor-content-ok',
            'doctor_content_edit.html': 'doctor-edit-ok',
        }),
        app.jinja_env.loader,
    ])


def test_only_doctor_can_create_draft_and_publish(db_session):
    doctor = _user('advice_doctor_ok', role='doctor')
    row = _draft(doctor)
    assert row.status == 'draft'
    assert row.author_user_id == doctor.id
    published = publish_advice(doctor, row.public_id)
    assert published.status == 'published'
    assert published.reviewer_user_id == doctor.id


def test_admin_create_draft_and_publish_raises_forbidden(db_session):
    doctor = _user('advice_doctor_for_admin', role='doctor')
    admin = _user('advice_admin_forbidden', role='admin')
    _assert_forbidden(lambda: _draft(admin))
    row = _draft(doctor)
    _assert_forbidden(lambda: publish_advice(admin, row.public_id))
    assert row.status == 'draft'


def test_regular_user_create_draft_and_publish_forbidden(db_session):
    doctor = _user('advice_doctor_for_user', role='doctor')
    regular = _user('advice_regular_user', role='user')
    _assert_forbidden(lambda: _draft(regular))
    row = _draft(doctor)
    _assert_forbidden(lambda: publish_advice(regular, row.public_id))


def test_draft_is_not_doctor_attributed(db_session):
    doctor = _user('advice_draft_attr', role='doctor')
    row = _draft(doctor)
    data = serialize_advice(row)
    assert data['doctor_attributed'] is False
    assert data['attribution_label'] == '平台规则提醒'
    assert '徐医生' not in data['attribution_label']


def test_publish_by_same_doctor_author_is_attributed(db_session):
    doctor = _user('advice_publish_attr', role='doctor')
    row = _draft(doctor)
    publish_advice(doctor, row.public_id)
    data = serialize_advice(row)
    assert data['doctor_attributed'] is True
    assert data['attribution_label'] == '徐医生已审核'


def test_admin_reviewer_is_not_doctor_attributed(db_session):
    doctor = _user('advice_reviewer_doctor', role='doctor')
    admin = _user('advice_reviewer_admin', role='admin')
    row = _draft(doctor)
    publish_advice(doctor, row.public_id)
    row.reviewer_user_id = admin.id
    db.session.flush()
    data = serialize_advice(row)
    assert data['doctor_attributed'] is False
    assert data['attribution_label'] == '平台规则提醒'

    now = utcnow()
    same_admin = AdviceContent(
        public_id='adminreviewer000000000000000001',
        kind='template',
        status='published',
        title='高温补水提醒',
        body=SAFE_BODY,
        source=SAFE_SOURCE,
        scenario='heat',
        version=1,
        author_user_id=admin.id,
        reviewer_user_id=admin.id,
        published_at=now,
        valid_from=now,
        created_at=now,
        updated_at=now,
    )
    db.session.add(same_admin)
    db.session.flush()
    admin_data = serialize_advice(same_admin)
    assert admin_data['doctor_attributed'] is False
    assert admin_data['attribution_label'] == '平台规则提醒'


def test_forbidden_claims_rejected_on_create_draft(db_session):
    doctor = _user('advice_forbidden_claim', role='doctor')
    markers = ('电费不到', '停药', '换药', '疾病概率')
    for marker in markers:
        try:
            _draft(doctor, body=f'提醒：{marker}，请注意。')
            assert False, f'{marker} should be rejected'
        except AdviceContentError as exc:
            assert exc.code == 'forbidden_claim'


def test_withdrawn_not_in_active_templates(db_session):
    doctor = _user('advice_withdraw', role='doctor')
    row = _draft(doctor)
    publish_advice(doctor, row.public_id)
    assert row.public_id in _public_ids(active_templates('heat'))
    withdraw_advice(doctor, row.public_id)
    assert row.status == 'withdrawn'
    assert row.public_id not in _public_ids(active_templates('heat'))


def test_expired_valid_until_not_currently_valid_or_active(db_session):
    doctor = _user('advice_expired', role='doctor')
    row = _draft(doctor, valid_until=utcnow() - timedelta(hours=1))
    publish_advice(doctor, row.public_id)
    data = serialize_advice(row)
    assert data['currently_valid'] is False
    assert row.public_id not in _public_ids(active_templates('heat'))


def test_unpublished_draft_not_in_active_templates(db_session):
    doctor = _user('advice_unpublished', role='doctor')
    row = _draft(doctor)
    assert row.status == 'draft'
    assert row.public_id not in _public_ids(active_templates('heat'))


def test_edit_published_increments_version_old_stays_until_withdrawn(db_session):
    doctor = _user('advice_version_edit', role='doctor')
    original = _draft(doctor, title='高温补水提醒 v1')
    publish_advice(doctor, original.public_id)
    original_data = serialize_advice(original)
    assert original_data['doctor_attributed'] is True
    assert original.public_id in _public_ids(active_templates('heat'))

    revision = _draft(
        doctor,
        title='高温补水提醒 v2',
        parent_id=original.public_id,
    )
    assert revision.version == int(original.version or 1) + 1
    assert revision.status == 'draft'
    assert original.status == 'published'
    revision_data = serialize_advice(revision)
    assert revision_data['doctor_attributed'] is False
    assert revision_data['attribution_label'] == '平台规则提醒'
    assert original.public_id in _public_ids(active_templates('heat'))
    assert revision.public_id not in _public_ids(active_templates('heat'))

    publish_advice(doctor, revision.public_id)
    assert serialize_advice(revision)['doctor_attributed'] is True
    assert original.status == 'published'
    assert original.public_id in _public_ids(active_templates('heat'))

    withdraw_advice(doctor, original.public_id)
    assert original.public_id not in _public_ids(active_templates('heat'))
    assert revision.public_id in _public_ids(active_templates('heat'))


def test_doctor_content_get_ok_admin_and_user_404(app, client, db_session):
    _stub_doctor_templates(app)
    with app.app_context():
        doctor = _user('advice_http_doctor', role='doctor')
        admin = _user('advice_http_admin', role='admin')
        regular = _user('advice_http_user', role='user')
        doctor_name, admin_name, user_name = doctor.username, admin.username, regular.username

    token = _csrf(client, 'advice-http-csrf')
    login = _login(client, doctor_name, token)
    assert login.status_code == 200
    page = client.get('/doctor/content')
    assert page.status_code == 200

    _logout(client, token)
    token = _csrf(client, 'advice-http-csrf')
    admin_login = _login(client, admin_name, token)
    assert admin_login.status_code == 200
    admin_page = client.get('/doctor/content')
    assert admin_page.status_code == 404

    _logout(client, token)
    token = _csrf(client, 'advice-http-csrf')
    user_login = _login(client, user_name, token)
    assert user_login.status_code == 200
    user_page = client.get('/doctor/content')
    assert user_page.status_code == 404


def test_doctor_content_new_post_with_csrf(app, client, db_session):
    _stub_doctor_templates(app)
    with app.app_context():
        doctor = _user('advice_http_post_doctor', role='doctor')
        username = doctor.username

    token = _csrf(client, 'advice-new-csrf')
    login = _login(client, username, token)
    assert login.status_code == 200
    response = client.post(
        '/doctor/content/new',
        data={
            'kind': 'template',
            'title': '高温补水提醒',
            'body': SAFE_BODY,
            'source': SAFE_SOURCE,
            'scenario': 'heat',
            'csrf_token': token,
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert AdviceContent.query.filter_by(title='高温补水提醒', status='draft').count() == 1
