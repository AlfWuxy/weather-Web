# -*- coding: utf-8 -*-
"""行动打卡只记录提交，不能暗示「已经安全」。"""

from datetime import timedelta
from pathlib import Path

from core.db_models import DailyStatus, Pair, PairActionToken, User
from core.extensions import db
from core.security import hash_pair_token, hash_short_code
from core.time_utils import today_local, utcnow

ROOT = Path(__file__).resolve().parents[1]


def test_action_checkin_submit_is_not_labeled_i_am_safe():
    html = (ROOT / 'templates/action_checkin.html').read_text(encoding='utf-8')
    assert '我很安全' not in html
    assert '记下今日情况' in html
    assert '不代表已经安全' in html
    assert '近7天风险与记录' in html
    assert '近7天风险与确认' not in html


def test_caregiver_pair_badge_records_submit_not_safety():
    html = (ROOT / 'templates/pair_management.html').read_text(encoding='utf-8')
    assert 'badge bg-success">已记录</span>' in html
    assert 'badge bg-success">已确认</span>' not in html


def test_action_confirm_without_checkboxes_records_view_not_completion(app, client):
    with app.app_context():
        db.create_all()
        user = User(username='checkin_honesty_user', role='user')
        user.set_password('pass1234')
        db.session.add(user)
        db.session.flush()

        pair = Pair(
            caregiver_id=user.id,
            community_code='都昌',
            location_query='都昌',
            elder_code='elder-checkin-honesty',
            short_code='55667788',
            short_code_hash=hash_short_code('55667788'),
            short_code_expires_at=utcnow() + timedelta(days=90),
            status='active',
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.session.add(pair)
        db.session.flush()
        db.session.add(PairActionToken(
            pair_id=pair.id,
            token_hash=hash_pair_token('checkin-honesty-token'),
            expires_at=utcnow() + timedelta(days=90),
            created_at=utcnow(),
        ))
        db.session.commit()
        pair_id = pair.id

    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'checkin-honesty-csrf'

    resp = client.post(
        '/e/checkin-honesty-token/checkin',
        data={'short_code': '55667788', 'csrf_token': 'checkin-honesty-csrf'},
        follow_redirects=False,
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert '我很安全' not in body
    assert '未勾选' in body
    assert '不代表已经安全' in body

    with app.app_context():
        status = DailyStatus.query.filter_by(pair_id=pair_id, status_date=today_local()).first()
        assert status is not None
        assert status.confirmed_at is not None
        assert status.actions_done_count == 0
