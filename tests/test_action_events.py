# -*- coding: utf-8 -*-
"""PRD-01 行动链：转移、幂等、求助推送、漏斗与老人端。"""
from datetime import timedelta

from core.db_models import ActionEvent, AlertDelivery, DailyStatus, Notification, Pair, User
from core.extensions import db
from core.security import hash_short_code
from core.time_utils import today_local, utcnow
from services.action_events import InvalidTransition, funnel, record_event, today_state


def _user(username='action_chain_user', role='user', **kwargs):
    user = User(username=username, role=role, **kwargs)
    user.set_password('pass12344')
    db.session.add(user)
    db.session.commit()
    return user


def _pair(user, code='80112233', elder_code='elder-action-chain', is_test=False):
    pair = Pair(
        caregiver_id=user.id,
        community_code='都昌',
        location_query='都昌',
        elder_code=elder_code,
        short_code=code,
        short_code_hash=hash_short_code(code),
        short_code_expires_at=utcnow() + timedelta(days=90),
        status='active',
        is_test=is_test,
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db.session.add(pair)
    db.session.commit()
    return pair


def _csrf(client, token='action-csrf'):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = token
    return token


def test_legal_transitions_full_path(db_session):
    user = _user('legal_path_user')
    pair = _pair(user, code='81111111', elder_code='elder-legal')
    now = utcnow()

    delivered = record_event(pair, 'delivered', 'system', 'wxpusher', now=now)
    seen = record_event(pair, 'seen', 'system', 'web_shortcode', now=now + timedelta(seconds=1))
    understood = record_event(pair, 'understood', 'elder', 'web_shortcode', now=now + timedelta(seconds=2))
    selected = record_event(
        pair,
        'action_selected',
        'elder',
        'web_shortcode',
        action_id='water',
        now=now + timedelta(seconds=3),
    )
    reported = record_event(
        pair,
        'self_reported',
        'elder',
        'web_shortcode',
        action_id='water',
        now=now + timedelta(seconds=4),
    )
    verified = record_event(
        pair,
        'caregiver_verified',
        'caregiver',
        'manual',
        now=now + timedelta(seconds=5),
    )
    closed = record_event(pair, 'closed', 'caregiver', 'manual', now=now + timedelta(seconds=6))

    assert ActionEvent.query.filter_by(pair_id=pair.id).count() == 7
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=today_local()).one()
    assert status.confirmed_at is not None
    assert status.understood_at is not None
    assert status.verified_at is not None
    assert status.closed_at is not None
    assert today_state(pair, today_local())['self_reported'] is True


def test_help_branch_and_illegal_transitions_do_not_persist(db_session):
    user = _user('illegal_path_user')
    pair = _pair(user, code='82222222', elder_code='elder-illegal')
    now = utcnow()
    before = ActionEvent.query.count()

    cases = [
        ('understood', 'elder', 'web_shortcode'),
        ('action_selected', 'elder', 'web_shortcode'),
        ('self_reported', 'elder', 'web_shortcode'),
        ('help_requested', 'elder', 'web_shortcode'),
        ('help_acknowledged', 'caregiver', 'manual'),
        ('caregiver_verified', 'caregiver', 'manual'),
        ('closed', 'caregiver', 'manual'),
        ('seen', 'elder', 'web_shortcode'),
        ('delivered', 'elder', 'manual'),
        ('help_acknowledged', 'elder', 'manual'),
    ]
    for stage, actor, channel in cases:
        try:
            record_event(pair, stage, actor, channel, now=now)
            assert False, f'{stage} should be illegal'
        except InvalidTransition as exc:
            assert exc.to_stage == stage
            body, status_code = exc.to_response()
            assert status_code == 400
            assert body.json['error'] == 'invalid_transition'

    assert ActionEvent.query.count() == before

    record_event(pair, 'seen', 'system', 'web_shortcode', now=now)
    record_event(pair, 'help_requested', 'elder', 'web_shortcode', now=now + timedelta(seconds=1))
    record_event(pair, 'help_acknowledged', 'caregiver', 'manual', now=now + timedelta(seconds=2))
    record_event(pair, 'closed', 'community', 'manual', now=now + timedelta(seconds=3))
    status = DailyStatus.query.filter_by(pair_id=pair.id, status_date=today_local()).one()
    assert status.help_flag is True
    assert status.help_acknowledged_at is not None
    assert status.confirmed_at is None


def test_http_illegal_understood_returns_400(app, client):
    with app.app_context():
        db.create_all()
        user = _user('http_illegal_user')
        pair = _pair(user, code='83333333', elder_code='elder-http-illegal')
        pair_id = pair.id

    token = _csrf(client)
    with client.session_transaction() as sess:
        sess['pair_session_id'] = pair_id
        sess['pair_session_code'] = '83333333'

    response = client.post(
        '/action/understood',
        data={'short_code': '83333333', 'csrf_token': token},
        headers={'Accept': 'application/json'},
    )
    assert response.status_code == 400
    assert response.get_json()['error'] == 'invalid_transition'
    with app.app_context():
        assert ActionEvent.query.filter_by(pair_id=pair_id).count() == 0


def test_sixty_second_idempotency(db_session):
    user = _user('idem_user')
    pair = _pair(user, code='84444444', elder_code='elder-idem')
    now = utcnow()
    record_event(pair, 'seen', 'system', 'web_shortcode', now=now)
    first = record_event(pair, 'understood', 'elder', 'web_shortcode', now=now + timedelta(seconds=1))
    second = record_event(pair, 'understood', 'elder', 'web_shortcode', now=now + timedelta(seconds=10))
    assert first.id == second.id
    assert ActionEvent.query.filter_by(pair_id=pair.id, stage='understood').count() == 1
    third = record_event(pair, 'understood', 'elder', 'web_shortcode', now=now + timedelta(seconds=70))
    assert third.id != first.id

    water = record_event(
        pair, 'self_reported', 'elder', 'web_shortcode', action_id='water', now=now + timedelta(seconds=80)
    )
    rest = record_event(
        pair, 'self_reported', 'elder', 'web_shortcode', action_id='rest', now=now + timedelta(seconds=81)
    )
    assert water.id != rest.id
    assert ActionEvent.query.filter_by(pair_id=pair.id, stage='self_reported').count() == 2


def test_action_help_http_notifies(app, client, monkeypatch):
    app.config['FEATURE_NOTIFICATIONS'] = True
    with app.app_context():
        db.create_all()
        user = _user(
            'help_http_user',
            wxpusher_uid='UID_HTTP',
            push_enabled=True,
        )
        pair = _pair(user, code='85550001', elder_code='elder-help-http')
        pair_id = pair.id

    called = []

    def fake_send(*_args, **_kwargs):
        called.append('send')
        raise RuntimeError('wxpusher down')

    monkeypatch.setattr('services.push.wxpusher.send', fake_send)

    token = _csrf(client, 'help-http-csrf')
    lookup = client.post('/action', data={'short_code': '85550001', 'csrf_token': token})
    assert lookup.status_code == 200
    response = client.post(
        '/action/help',
        data={'short_code': '85550001', 'csrf_token': token},
    )
    assert response.status_code == 200
    with app.app_context():
        assert ActionEvent.query.filter_by(pair_id=pair_id, stage='help_requested').count() == 1
        assert Notification.query.filter_by(category='help_requested').count() >= 1
        assert DailyStatus.query.filter_by(pair_id=pair_id).one().help_flag is True
        failed = AlertDelivery.query.filter_by(pair_id=pair_id, channel='wxpusher').all()
        assert failed
        assert all(row.status == 'failed' for row in failed)
        assert called


def test_help_requested_notifies_and_survives_wxpusher_failure(app, db_session, monkeypatch):
    app.config['FEATURE_NOTIFICATIONS'] = True
    user = _user(
        'help_push_user',
        wxpusher_uid='UID_HELP',
        push_enabled=True,
    )
    pair = _pair(user, code='85555555', elder_code='elder-help-push')
    now = utcnow()
    record_event(pair, 'seen', 'system', 'web_shortcode', now=now)

    def boom(*_args, **_kwargs):
        raise RuntimeError('wxpusher down')

    monkeypatch.setattr('services.push.wxpusher.send', boom)

    from services.public_service import _notify_help_requested

    record_event(pair, 'help_requested', 'elder', 'web_shortcode', now=now + timedelta(seconds=1))
    _notify_help_requested(pair)

    assert Notification.query.filter_by(user_id=user.id, category='help_requested').count() >= 1
    assert ActionEvent.query.filter_by(pair_id=pair.id, stage='help_requested').count() == 1
    deliveries = AlertDelivery.query.filter_by(pair_id=pair.id, channel='wxpusher').all()
    assert deliveries
    assert all(row.status == 'failed' for row in deliveries)


def test_verified_without_self_report_meta(db_session):
    user = _user('verified_meta_user')
    pair = _pair(user, code='86666666', elder_code='elder-verified-meta')
    now = utcnow()
    record_event(pair, 'delivered', 'caregiver', 'manual', now=now)
    event = record_event(pair, 'caregiver_verified', 'caregiver', 'manual', now=now + timedelta(seconds=1))
    import json
    meta = json.loads(event.meta_json)
    assert meta['verified_without_self_report'] is True


def test_funnel_denominator_and_unknown(db_session):
    real_user = _user('funnel_real')
    qa_user = _user('qa_hidden')
    real_pair = _pair(real_user, code='87777701', elder_code='elder-funnel-real')
    _pair(qa_user, code='87777702', elder_code='qa_hidden_pair')
    _pair(real_user, code='87777703', elder_code='elder-funnel-unknown')
    now = utcnow()
    record_event(real_pair, 'seen', 'system', 'web_shortcode', now=now)

    today = today_local()
    result = funnel(today, today, include_test=False)
    assert result['denominator'] == 2
    assert result['unknown_count'] == 1
    assert result['stages']['seen']['pairs'] == 1
    assert result['stages']['seen']['denominator'] == 2

    with_test = funnel(today, today, include_test=True)
    assert with_test['denominator'] >= 3


def test_export_csv_has_no_raw_pair_id(app, client, db_session):
    admin = _user('admin_export_action', role='admin')
    user = _user('export_owner')
    pair = _pair(user, code='88888801', elder_code='elder-export')
    record_event(pair, 'seen', 'system', 'web_shortcode')

    with client.session_transaction() as sess:
        sess['_user_id'] = admin.get_id()
        sess['_fresh'] = True
        sess['_csrf_token'] = 'export-csrf'

    response = client.get('/analysis/pilot/export.csv?days=30')
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    header = text.splitlines()[0].lstrip('\ufeff')
    columns = header.split(',')
    assert columns[0] == 'pair_hash'
    assert 'pair_id' not in columns
    assert user.username not in text
    assert 'elder-export' not in text


def test_elder_mode_renders_three_buttons_and_accepts_posts(app, client, db_session):
    user = _user('elder_mode_actor')
    pair = _pair(user, code='89999901', elder_code='elder-mode-pair')
    token = _csrf(client, 'elder-mode-csrf')
    client.post('/login', data={'username': 'elder_mode_actor', 'password': 'pass12344', 'csrf_token': token})

    page = client.get('/elder-mode')
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert '我看懂了' in body
    assert '我做到一项' in body
    assert '做不到，需要帮助' in body
    assert '我很安全' not in body

    understood = client.post(
        '/elder-mode/understood',
        json={'csrf_token': token},
        headers={'X-CSRF-Token': token, 'Accept': 'application/json'},
    )
    assert understood.status_code == 200
    assert understood.get_json()['ok'] is True

    selected = client.post(
        '/elder-mode/select',
        json={'csrf_token': token, 'action_id': 'undecided'},
        headers={'X-CSRF-Token': token, 'Accept': 'application/json'},
    )
    assert selected.status_code == 200

    reported = client.post(
        '/elder-mode/confirm',
        json={'csrf_token': token, 'actions_done': ['water']},
        headers={'X-CSRF-Token': token, 'Accept': 'application/json'},
    )
    assert reported.status_code == 200
    assert ActionEvent.query.filter_by(pair_id=pair.id, stage='self_reported').count() >= 1


def test_misclick_suspect_after_help(db_session):
    user = _user('misclick_user')
    pair = _pair(user, code='89990011', elder_code='elder-misclick')
    now = utcnow()
    record_event(pair, 'seen', 'system', 'web_shortcode', now=now)
    record_event(pair, 'help_requested', 'elder', 'web_shortcode', now=now + timedelta(seconds=1))
    understood = record_event(
        pair, 'understood', 'elder', 'web_shortcode', now=now + timedelta(seconds=10)
    )
    import json
    assert json.loads(understood.meta_json)['misclick_suspect'] is True


def test_caregiver_closed_without_predecessor_http_400(app, client, db_session):
    user = _user('cg_closed_actor')
    pair = _pair(user, code='89990022', elder_code='elder-cg-closed')
    token = _csrf(client, 'cg-closed-csrf')
    client.post(
        '/login',
        data={'username': 'cg_closed_actor', 'password': 'pass12344', 'csrf_token': token},
    )
    response = client.post(
        f'/caregiver/pair/{pair.id}/action-log',
        json={'event': 'closed', 'csrf_token': token},
        headers={'X-CSRF-Token': token, 'Accept': 'application/json'},
    )
    assert response.status_code == 400
    assert response.get_json()['error'] == 'invalid_transition'
    assert ActionEvent.query.filter_by(pair_id=pair.id).count() == 0
