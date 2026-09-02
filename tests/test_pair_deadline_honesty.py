# -*- coding: utf-8 -*-


def test_pair_page_explains_confirm_deadline_and_backup_escalate(authenticated_client):
    response = authenticated_client.get('/pairs')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '每日 20:00 前确认' in html
    assert '2 小时未确认会转备用联系人' in html


def test_pair_page_shows_backup_clock_for_unconfirmed_status(authenticated_client, db_session):
    from core.db_models import DailyStatus, Pair, User
    from core.security import hash_short_code
    from core.time_utils import today_local, utcnow

    user = User.query.filter_by(username='testuser').one()
    pair = Pair(
        caregiver_id=user.id,
        community_code='都昌',
        location_query='都昌',
        elder_code='elder-deadline',
        short_code='87654321',
        short_code_hash=hash_short_code('87654321'),
        status='active',
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.commit()
    db_session.add(DailyStatus(
        pair_id=pair.id,
        status_date=today_local(),
        community_code='都昌',
        created_at=utcnow(),
        relay_stage='none',
    ))
    db_session.commit()

    html = authenticated_client.get('/pairs').get_data(as_text=True)
    assert '今日确认' in html
    assert '转备用' in html
    assert 'data-deadline-hour="20"' in html
    assert 'data-deadline=' in html


def test_pair_escalate_buttons_use_the_same_label(authenticated_client, db_session):
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow

    user = User.query.filter_by(username='testuser').one()
    pair = Pair(
        caregiver_id=user.id,
        community_code='都昌',
        location_query='都昌',
        elder_code='elder-escalate-label',
        short_code='87654322',
        short_code_hash=hash_short_code('87654322'),
        status='active',
        created_at=utcnow(),
        last_active_at=utcnow(),
    )
    db_session.add(pair)
    db_session.commit()

    list_html = authenticated_client.get('/pairs').get_data(as_text=True)
    assert '升级到下一档' in list_html

    user.role = 'caregiver'
    db_session.commit()
    detail_html = authenticated_client.get(f'/caregiver/pair/{pair.id}').get_data(as_text=True)
    assert '升级到下一档' in detail_html
    assert '升级链推进' not in detail_html
