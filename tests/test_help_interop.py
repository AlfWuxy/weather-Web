# -*- coding: utf-8 -*-
"""已知回归必须在旧实现失败、新实现通过：跨天求助、再求助、表单地址、家庭邀请、幂等。"""
from datetime import timedelta

from core.db_models import (
    FamilyMembership,
    HelpRequest,
    NotificationOutbox,
    Pair,
    User,
)
from core.extensions import db
from core.security import hash_short_code
from core.time_utils import utcnow
from core.usage import create_api_token
from services.action_events import record_event
from services.family_access import create_invite, consume_invite, preview_invite
from services.help_request_service import (
    ack_help_request,
    create_help_request,
    list_help_requests,
    resolve_help_request,
)
from services.notification_outbox import process_outbox_batch


def _user(username, role='user'):
    user = User(username=username, role=role)
    user.set_password('pass12344')
    db.session.add(user)
    db.session.commit()
    return user


def _pair(user, code='90110001', elder_code='elder-help-interop'):
    pair = Pair(
        caregiver_id=user.id,
        community_code='都昌',
        location_query='都昌',
        elder_code=elder_code,
        short_code=code,
        short_code_hash=hash_short_code(code),
        status='active',
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db.session.add(pair)
    db.session.commit()
    return pair


def _csrf(client, token='help-csrf'):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = token
    return token


def _auth(user_id):
    return {'Authorization': f'Bearer {create_api_token(user_id, name="help-interop")}'}


def test_action_page_help_form_does_not_post_to_none(app, client, db_session):
    with app.app_context():
        user = _user('form_none_user')
        pair = _pair(user, code='90111111', elder_code='elder-form-none')
        code = pair.short_code

    token = _csrf(client)
    response = client.post(
        '/action',
        data={'short_code': code, 'csrf_token': token},
        follow_redirects=True,
    )
    html = response.get_data(as_text=True)
    assert 'action="None"' not in html
    assert 'action="/None"' not in html
    assert '/action/help' in html


def test_yesterday_help_stays_open_and_can_be_acked(db_session):
    owner = _user('cross_day_owner')
    pair = _pair(owner, code='90112222', elder_code='elder-cross-day')
    yesterday = utcnow() - timedelta(hours=26)
    record_event(pair, 'seen', 'system', 'web_shortcode', now=yesterday, commit=True)
    body, created = create_help_request(
        owner,
        pair,
        origin_channel='web',
        category='cannot_complete',
        is_proxy=True,
        commit=True,
    )
    assert created is True
    public_id = body['id']

    listed = list_help_requests(owner, status='open')
    assert listed['open_count'] == 1
    assert listed['items'][0]['id'] == public_id
    assert listed['items'][0]['status'] == 'pending_ack'

    acked = ack_help_request(owner, public_id, expected_version=body['version'], origin_channel='web', commit=True)
    assert acked['status'] == 'acknowledged'
    assert acked['resolved_at'] is None


def test_resolve_then_new_help_gets_new_id(db_session):
    owner = _user('rehelp_owner')
    pair = _pair(owner, code='90113333', elder_code='elder-rehelp')
    first, _ = create_help_request(owner, pair, origin_channel='miniprogram', is_proxy=True, commit=True)
    ack_help_request(owner, first['id'], expected_version=first['version'], commit=True)
    latest = HelpRequest.query.filter_by(public_id=first['id']).one()
    resolved = resolve_help_request(
        owner,
        first['id'],
        expected_version=latest.version,
        resolution_code='reached_elder',
        commit=True,
    )
    assert resolved['status'] == 'resolved'
    second, created = create_help_request(
        owner,
        pair,
        origin_channel='miniprogram',
        is_proxy=True,
        idempotency_key='rehelp-2',
        commit=True,
    )
    assert created is True
    assert second['id'] != first['id']
    assert second['status'] == 'pending_ack'
    listed = list_help_requests(owner, status='open')
    assert listed['open_count'] == 1
    assert listed['items'][0]['id'] == second['id']


def test_second_create_reminds_existing_open_request(db_session):
    owner = _user('remind_owner')
    pair = _pair(owner, code='90114444', elder_code='elder-remind')
    first, _ = create_help_request(owner, pair, origin_channel='miniprogram', is_proxy=True, commit=True)
    second, created = create_help_request(owner, pair, origin_channel='miniprogram', is_proxy=True, commit=True)
    assert created is False
    assert second['id'] == first['id']
    assert second.get('replayed') is True
    assert HelpRequest.query.filter_by(pair_id=pair.id).count() == 1


def test_idempotency_same_key_replays_and_mismatch_rejected(db_session):
    owner = _user('idem_owner')
    pair = _pair(owner, code='90115555', elder_code='elder-idem')
    first, _ = create_help_request(
        owner,
        pair,
        origin_channel='miniprogram',
        is_proxy=True,
        idempotency_key='same-key',
        commit=True,
    )
    replay, created = create_help_request(
        owner,
        pair,
        origin_channel='miniprogram',
        is_proxy=True,
        idempotency_key='same-key',
        commit=True,
    )
    assert created is False
    assert replay['id'] == first['id']
    try:
        create_help_request(
            owner,
            pair,
            origin_channel='web',
            is_proxy=True,
            idempotency_key='same-key',
            commit=True,
        )
        assert False, 'mismatch should reject'
    except Exception as exc:
        assert getattr(exc, 'code', '') == 'idempotency_mismatch'


def test_version_conflict_does_not_overwrite(db_session):
    owner = _user('conflict_owner')
    other = _user('conflict_other')
    pair = _pair(owner, code='90116666', elder_code='elder-conflict')
    from services.family_access import ensure_space_for_pair, ROLE_CAREGIVER, ACTIVE
    from core.db_models import FamilyMembership

    space = ensure_space_for_pair(pair, commit=True)
    db.session.add(FamilyMembership(
        family_space_id=space.id,
        user_id=other.id,
        role=ROLE_CAREGIVER,
        status=ACTIVE,
        invited_by_user_id=owner.id,
        created_at=utcnow(),
    ))
    db.session.commit()
    created, _ = create_help_request(owner, pair, origin_channel='web', is_proxy=True, commit=True)
    ack_help_request(owner, created['id'], expected_version=created['version'], commit=True)
    try:
        ack_help_request(other, created['id'], expected_version=created['version'], commit=True)
        assert False, 'stale version should conflict'
    except Exception as exc:
        assert getattr(exc, 'code', '') == 'version_conflict'


def test_other_family_cannot_read_help(db_session):
    owner = _user('owner_a')
    stranger = _user('owner_b')
    pair = _pair(owner, code='90117777', elder_code='elder-owner-a')
    _pair(stranger, code='90117778', elder_code='elder-owner-b')
    created, _ = create_help_request(owner, pair, origin_channel='web', is_proxy=True, commit=True)
    listed = list_help_requests(stranger, status='open')
    assert all(item['id'] != created['id'] for item in listed['items'])
    from services.help_request_service import get_help_request, HelpRequestError
    try:
        get_help_request(stranger, created['id'])
        assert False, 'should hide foreign help'
    except HelpRequestError as exc:
        assert exc.status_code == 404


def test_invite_preview_does_not_consume_and_replay_fails(db_session):
    owner = _user('invite_owner')
    joiner = _user('invite_joiner')
    pair = _pair(owner, code='90118888', elder_code='elder-invite')
    invite, plain = create_invite(owner, pair, 'caregiver', ttl_hours=2, max_uses=1)
    db.session.commit()
    preview = preview_invite(plain)
    assert preview['consumes'] is False
    assert preview['role'] == 'caregiver'
    assert FamilyMembership.query.filter_by(user_id=joiner.id).count() == 0
    consume_invite(joiner, plain)
    db.session.commit()
    assert FamilyMembership.query.filter_by(user_id=joiner.id, status='active').count() == 1
    from services.family_access import FamilyAccessError
    try:
        consume_invite(joiner, plain)
        assert False, 'replay should fail'
    except FamilyAccessError:
        pass
    assert FamilyMembership.query.filter_by(user_id=joiner.id, status='active').count() == 1


def test_outbox_and_help_rollback_together(db_session):
    owner = _user('tx_owner')
    pair = _pair(owner, code='90119999', elder_code='elder-tx')
    create_help_request(owner, pair, origin_channel='web', is_proxy=True, commit=False)
    assert HelpRequest.query.count() == 1
    assert NotificationOutbox.query.count() >= 1
    db.session.rollback()
    assert HelpRequest.query.count() == 0
    assert NotificationOutbox.query.count() == 0


def test_mp_help_requests_endpoint_and_pending_open(app, client, db_session):
    with app.app_context():
        owner = _user('mp_help_owner')
        pair = _pair(owner, code='90120001', elder_code='elder-mp-help')
        headers = _auth(owner.id)
        pair_id = pair.id

    caps = client.get('/mp/api/v1/capabilities', headers=headers)
    assert caps.status_code == 200
    assert caps.get_json()['data']['features']['help_requests'] is True

    scripts = client.get('/mp/api/v1/scripts', headers=headers)
    assert scripts.status_code == 200
    assert scripts.get_json()['data']['version_hash']

    created = client.post(
        '/mp/api/v1/help-requests',
        json={'pair_id': pair_id, 'category': 'cannot_complete', 'idempotency_key': 'mp-1'},
        headers=headers,
    )
    assert created.status_code == 200
    help_id = created.get_json()['data']['id']

    pending = client.get('/mp/api/v1/pending', headers=headers)
    assert pending.status_code == 200
    body = pending.get_json()
    assert body['data']['open_count'] >= 1
    ids = [item['id'] for item in body['data']['help_requests']]
    assert help_id in ids

    compat = client.post(
        f'/mp/api/v1/actions/{pair_id}/help',
        json={'idempotency_key': 'mp-compat'},
        headers=headers,
    )
    assert compat.status_code == 200
    assert compat.get_json()['data']['id'] == help_id


def test_outbox_worker_marks_sandbox_without_dropping_help(db_session):
    owner = _user('outbox_owner', role='user')
    owner.wxpusher_uid = 'UID_TEST'
    owner.push_enabled = True
    db.session.commit()
    pair = _pair(owner, code='90121111', elder_code='elder-outbox')
    create_help_request(owner, pair, origin_channel='web', is_proxy=True, commit=True)
    processed = process_outbox_batch(limit=20)
    assert processed >= 1
    assert HelpRequest.query.count() == 1
    rows = NotificationOutbox.query.all()
    assert rows
    assert all(row.status in {'accepted', 'pending', 'failed'} for row in rows)
    assert not any(row.status == 'sent_user_confirmed' for row in rows)


def test_invite_expired_is_rejected(db_session):
    owner = _user('expire_owner')
    joiner = _user('expire_joiner')
    pair = _pair(owner, code='90122222', elder_code='elder-expire')
    invite, plain = create_invite(owner, pair, 'caregiver', ttl_hours=1, max_uses=1)
    invite.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    from services.family_access import FamilyAccessError
    preview = preview_invite(plain)
    assert preview['status'] == 'expired'
    try:
        consume_invite(joiner, plain)
        assert False, 'expired invite must not consume'
    except FamilyAccessError as exc:
        assert exc.code == 'invite_inactive'
    assert FamilyMembership.query.filter_by(user_id=joiner.id, status='active').count() == 0


def test_bootstrap_is_cache_only_and_stale_is_not_low_risk(app, client, db_session, monkeypatch):
    called = {'weather': 0}

    def boom(*_args, **_kwargs):
        called['weather'] += 1
        raise AssertionError('bootstrap must not fetch vendor weather')

    monkeypatch.setattr('core.weather.get_weather_with_cache', boom)
    response = client.get('/mp/api/v1/bootstrap')
    assert response.status_code == 200
    data = response.get_json()['data']
    assert data['stale'] is True or data['available'] is False
    assert data['risk']['level'] != '低风险'
    assert called['weather'] == 0


def test_backfill_dry_run_is_reentrant(db_session):
    from scripts.backfill_family_help import run_backfill

    owner = _user('backfill_owner')
    pair = _pair(owner, code='90123333', elder_code='elder-backfill')
    from core.db_models import DailyStatus
    from core.time_utils import today_local
    db.session.add(DailyStatus(
        pair_id=pair.id,
        status_date=today_local(),
        community_code=pair.community_code,
        help_flag=True,
        actions_done_count=0,
        relay_stage='none',
        created_at=utcnow(),
    ))
    db.session.commit()
    first = run_backfill(dry_run=True)
    second = run_backfill(dry_run=True)
    assert first['pairs_seen'] >= 1
    assert HelpRequest.query.count() == 0
    assert first['open_help_created'] == second['open_help_created']


def test_web_help_inbox_lists_open_request(app, client, db_session):
    with app.app_context():
        owner = _user('inbox_page_owner')
        pair = _pair(owner, code='90126666', elder_code='elder-inbox-page')
        created, _ = create_help_request(owner, pair, origin_channel='miniprogram', is_proxy=True, commit=True)
        help_id = created['id']
        username = owner.username

    token = _csrf(client, 'inbox-page-csrf')
    login = client.post(
        '/login',
        data={'username': username, 'password': 'pass12344', 'csrf_token': token},
        follow_redirects=True,
    )
    assert login.status_code == 200
    page = client.get('/caregiver/help')
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert '待处理求助' in html
    assert help_id in html
    assert 'action="/None"' not in html
    listed = client.get('/api/v1/help-requests?status=open')
    assert listed.status_code == 200
    assert listed.get_json()['data']['items'][0]['id'] == help_id


def test_help_event_labels_are_chinese(db_session):
    from services.help_request_service import get_help_request

    owner = _user('label_owner')
    pair = _pair(owner, code='90124444', elder_code='elder-label')
    created, _ = create_help_request(owner, pair, origin_channel='web', is_proxy=True, commit=True)
    detail = get_help_request(owner, created['id'])
    assert detail['events']
    assert detail['events'][0]['type_label'] == '已发起求助'
    assert detail['status_label'] == '待家属接收'
    assert 'pending_ack' not in detail['events'][0]['type_label']


def test_invite_revoked_is_rejected(db_session):
    owner = _user('revoke_owner')
    joiner = _user('revoke_joiner')
    pair = _pair(owner, code='90125555', elder_code='elder-revoke')
    invite, plain = create_invite(owner, pair, 'caregiver', ttl_hours=2, max_uses=1)
    db.session.commit()
    from services.family_access import FamilyAccessError, revoke_invite
    revoke_invite(owner, invite.id)
    db.session.commit()
    preview = preview_invite(plain)
    assert preview['status'] == 'revoked'
    try:
        consume_invite(joiner, plain)
        assert False, 'revoked invite must not consume'
    except FamilyAccessError as exc:
        assert exc.code == 'invite_inactive'
    assert FamilyMembership.query.filter_by(user_id=joiner.id, status='active').count() == 0


def test_invite_concurrent_consume_at_most_one(app, db_session):
    import threading

    with app.app_context():
        owner = _user('conc_owner')
        first = _user('conc_first')
        second = _user('conc_second')
        pair = _pair(owner, code='90126666', elder_code='elder-conc')
        _invite, plain = create_invite(owner, pair, 'caregiver', ttl_hours=2, max_uses=1)
        db.session.commit()
        first_id, second_id, code = first.id, second.id, plain

    barrier = threading.Barrier(2)
    outcomes = []

    def worker(user_id):
        with app.app_context():
            user = db.session.get(User, user_id)
            barrier.wait(timeout=5)
            try:
                consume_invite(user, code)
                db.session.commit()
                outcomes.append('ok')
            except Exception as exc:
                db.session.rollback()
                outcomes.append(getattr(exc, 'code', type(exc).__name__))

    threads = [
        threading.Thread(target=worker, args=(first_id,)),
        threading.Thread(target=worker, args=(second_id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert outcomes.count('ok') == 1
    with app.app_context():
        members = FamilyMembership.query.filter(
            FamilyMembership.user_id.in_([first_id, second_id]),
            FamilyMembership.status == 'active',
        ).count()
        assert members == 1


def test_mp_create_then_web_list_same_id_under_10s(app, client, db_session):
    import time

    with app.app_context():
        owner = _user('sync_owner')
        pair = _pair(owner, code='90127777', elder_code='elder-sync')
        headers = _auth(owner.id)
        pair_id = pair.id

    csrf = _csrf(client)
    login = client.post(
        '/login',
        data={'username': 'sync_owner', 'password': 'pass12344', 'csrf_token': csrf},
        follow_redirects=True,
    )
    assert login.status_code == 200

    started = time.perf_counter()
    created = client.post(
        '/mp/api/v1/help-requests',
        json={'pair_id': pair_id, 'category': 'cannot_complete', 'idempotency_key': 'sync-1'},
        headers=headers,
    )
    assert created.status_code == 200
    help_id = created.get_json()['data']['id']

    listed = client.get('/api/v1/help-requests?status=open&limit=20')
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert listed.status_code == 200
    payload = listed.get_json()
    assert payload and payload.get('success') is True
    ids = [item['id'] for item in payload['data']['items']]
    assert help_id in ids
    assert elapsed_ms < 10000


def test_outbox_retry_after_restart_does_not_duplicate(db_session, monkeypatch):
    owner = _user('outbox_retry_owner')
    owner.wxpusher_uid = 'UID_RETRY'
    owner.push_enabled = True
    db.session.commit()
    pair = _pair(owner, code='90128888', elder_code='elder-outbox-retry')
    created, _ = create_help_request(owner, pair, origin_channel='web', is_proxy=True, commit=True)
    first = process_outbox_batch(limit=20)
    second = process_outbox_batch(limit=20)
    rows = NotificationOutbox.query.filter_by(help_request_id=HelpRequest.query.filter_by(public_id=created['id']).one().id).all()
    assert first >= 1
    assert HelpRequest.query.filter_by(public_id=created['id']).count() == 1
    assert len({row.dedupe_key for row in rows}) == len(rows)
    assert second >= 0
