# -*- coding: utf-8 -*-
"""普通安全边界回归测试。"""


def test_default_responses_include_browser_security_headers(client):
    response = client.get('/')

    assert response.status_code == 200
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert response.headers['Referrer-Policy'] == 'no-referrer'
    assert response.headers['Permissions-Policy'] == (
        'camera=(), microphone=(), geolocation=()'
    )


def test_default_request_body_limit_is_enabled(app):
    assert app.config['MAX_CONTENT_LENGTH'] == 1024 * 1024


def test_request_body_limit_rejects_oversized_json(app, authenticated_client):
    app.config['MAX_CONTENT_LENGTH'] = 1024
    with authenticated_client.session_transaction() as session:
        session['_csrf_token'] = 'oversized-body-csrf'

    response = authenticated_client.post(
        '/api/v1/events',
        json={
            'event_type': 'template_view',
            'meta': {'payload': 'x' * 2048},
        },
        headers={'X-CSRF-Token': 'oversized-body-csrf'},
    )

    assert response.status_code == 413


def test_public_model_status_hides_internal_runtime_details(
    client,
    monkeypatch,
):
    class MlServiceStub:
        def get_model_status(self):
            return {
                'model_loaded': False,
                'loaded': False,
                'model_name': 'test-model',
                'error': '/private/model.pkl failed to load',
                'feature_cols': ['secret_feature'],
                'runtime_sklearn_version': '1.2.3',
            }

    monkeypatch.setattr(
        'services.ml_prediction_service.get_ml_service',
        lambda: MlServiceStub(),
    )

    response = client.get('/api/v1/ml/status')
    status = response.get_json()['status']

    assert response.status_code == 200
    assert status['availability'] == 'unavailable'
    assert status['model_name'] == 'test-model'
    assert 'error' not in status
    assert 'feature_cols' not in status
    assert 'runtime_sklearn_version' not in status


def test_public_dlnm_summary_hides_profile_path(client, monkeypatch):
    class DlnmServiceStub:
        def get_model_summary(self):
            return {
                'status': '模型已训练',
                'profile_name': 'public-profile',
                'profile_path': '/private/model/profile.json',
            }

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: DlnmServiceStub(),
    )

    response = client.get('/api/v1/dlnm/summary')
    summary = response.get_json()['summary']

    assert response.status_code == 200
    assert summary['profile_name'] == 'public-profile'
    assert 'profile_path' not in summary


def test_web_events_reject_non_object_and_server_owned_event(
    authenticated_client,
):
    with authenticated_client.session_transaction() as session:
        session['_csrf_token'] = 'event-security-csrf'

    invalid_payload = authenticated_client.post(
        '/api/v1/events',
        json=[],
        headers={'X-CSRF-Token': 'event-security-csrf'},
    )
    forged_event = authenticated_client.post(
        '/api/v1/events',
        json={'event_type': 'push_sent'},
        headers={'X-CSRF-Token': 'event-security-csrf'},
    )

    assert invalid_payload.status_code == 400
    assert invalid_payload.get_json()['error'] == 'invalid_payload'
    assert forged_event.status_code == 400
    assert forged_event.get_json()['error'] == 'invalid event_type'


def test_web_events_reject_implicitly_converted_and_false_relation_ids(
    app,
    authenticated_client,
    db_session,
):
    from core.db_models import FamilyMember, Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow

    with app.app_context():
        user = User.query.filter_by(username='testuser').one()
        member = FamilyMember(
            user_id=user.id,
            name='妈妈',
            relation='母亲',
            age=68,
            gender='女性',
            created_at=utcnow(),
        )
        db_session.add(member)
        db_session.flush()
        pair = Pair(
            caregiver_id=user.id,
            community_code='都昌',
            location_query='都昌',
            elder_code='event-link-elder',
            short_code='13571357',
            short_code_hash=hash_short_code('13571357'),
            status='active',
            last_active_at=utcnow(),
        )
        db_session.add(pair)
        db_session.commit()
        pair_id = pair.id
        member_id = member.id

    with authenticated_client.session_transaction() as session:
        session['_csrf_token'] = 'event-relation-csrf'
    headers = {'X-CSRF-Token': 'event-relation-csrf'}

    boolean_id = authenticated_client.post(
        '/api/v1/events',
        json={'event_type': 'template_view', 'pair_id': True},
        headers=headers,
    )
    decimal_id = authenticated_client.post(
        '/api/v1/events',
        json={'event_type': 'template_view', 'member_id': 1.5},
        headers=headers,
    )
    false_relation = authenticated_client.post(
        '/api/v1/events',
        json={
            'event_type': 'template_view',
            'pair_id': pair_id,
            'member_id': member_id,
        },
        headers=headers,
    )

    assert boolean_id.status_code == 400
    assert boolean_id.get_json()['error'] == 'invalid_pair_id'
    assert decimal_id.status_code == 400
    assert decimal_id.get_json()['error'] == 'invalid_member_id'
    assert false_relation.status_code == 400
    assert false_relation.get_json()['error'] == 'member_pair_mismatch'


def test_action_link_uses_configured_public_base_url(
    app,
    db_session,
):
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from services.user._common import _build_pair_action_link

    with app.app_context():
        user = User(username='action_link_owner', role='user')
        user.set_password('pw123456')
        db_session.add(user)
        db_session.commit()

        pair = Pair(
            caregiver_id=user.id,
            community_code='都昌',
            location_query='都昌',
            elder_code='action-link-elder',
            short_code='24682468',
            short_code_hash=hash_short_code('24682468'),
            status='active',
            last_active_at=utcnow(),
        )
        db_session.add(pair)
        db_session.commit()
        pair_id = pair.id

        app.config['PUBLIC_BASE_URL'] = 'https://trusted.example'
        with app.test_request_context('/pairs', base_url='https://evil.example'):
            pair = db_session.get(Pair, pair_id)
            action_link = _build_pair_action_link(pair, external=True)

    assert action_link.startswith('https://trusted.example/e/')
    assert 'evil.example' not in action_link


def test_action_link_fails_closed_without_valid_public_base_url(
    app,
    db_session,
):
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from services.user._common import _build_pair_action_link

    with app.app_context():
        user = User(username='closed_action_link_owner', role='user')
        user.set_password('pw123456')
        db_session.add(user)
        db_session.flush()
        pair = Pair(
            caregiver_id=user.id,
            community_code='都昌',
            location_query='都昌',
            elder_code='closed-action-link-elder',
            short_code='86428642',
            short_code_hash=hash_short_code('86428642'),
            status='active',
            last_active_at=utcnow(),
        )
        db_session.add(pair)
        db_session.commit()
        pair_id = pair.id

        for invalid_base in (
            '',
            'javascript:alert(1)',
            'https://user:password@trusted.example',
            'https://trusted.example?redirect=evil',
            'https://trusted.example/#fragment',
        ):
            app.config['PUBLIC_BASE_URL'] = invalid_base
            with app.test_request_context(
                '/pairs',
                base_url='https://evil.example',
            ):
                pair = db_session.get(Pair, pair_id)
                link = _build_pair_action_link(pair, external=True)
            assert link.startswith('/e/')
            assert 'evil.example' not in link

        app.config['PUBLIC_BASE_URL'] = 'https://trusted.example/base/'
        with app.test_request_context('/pairs', base_url='https://evil.example'):
            pair = db_session.get(Pair, pair_id)
            prefixed_link = _build_pair_action_link(pair, external=True)
            relative_link = _build_pair_action_link(pair, external=False)

    assert prefixed_link.startswith('https://trusted.example/base/e/')
    assert relative_link.startswith('/e/')
