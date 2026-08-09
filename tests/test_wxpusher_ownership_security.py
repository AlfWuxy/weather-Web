# -*- coding: utf-8 -*-
"""WxPusher 所有权与重试上限安全回归。"""

import pytest
from sqlalchemy.exc import IntegrityError


def test_wxpusher_uid_is_unique_at_database_layer(db_session):
    from core.db_models import User

    users = [
        User(username='wx_unique_one', role='user', wxpusher_uid='UID_SHARED'),
        User(username='wx_unique_two', role='user', wxpusher_uid='UID_SHARED'),
    ]
    for user in users:
        user.set_password('UserPassword1!')
    db_session.add_all(users)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_delivery_cannot_exceed_one_manual_retry(app, db_session):
    from core.db_models import AlertDelivery, User, WeatherAlert
    from core.time_utils import utcnow
    from services.push.dispatch import _claim_delivery

    app.config['WXPUSHER_MAX_DELIVERY_ATTEMPTS'] = 2
    user = User(username='wx_retry_limit', role='user')
    user.set_password('UserPassword1!')
    db_session.add(user)
    alert = WeatherAlert(
        alert_date=utcnow(),
        location='116.20,29.27',
        alert_type='heat_threshold',
        alert_level='阈值',
        description='test',
        affected_communities='[]',
        disease_correlation='{}',
    )
    db_session.add(alert)
    db_session.flush()
    delivery = AlertDelivery(
        alert_id=alert.id,
        user_id=user.id,
        channel='wxpusher',
        status='retry_ready',
        delivery_token='retry-limit-token',
        sent_at=utcnow(),
        attempt_count=2,
        review_action='allow_retry',
        reviewed_at=utcnow(),
        reviewed_by_user_id=user.id,
    )
    db_session.add(delivery)
    db_session.commit()

    claim = _claim_delivery(
        alert_id=alert.id,
        user_id=user.id,
        pair_id=None,
        now=utcnow(),
    )

    assert claim['action'] == 'skip'
    assert claim['state'] == 'failed'
    db_session.expire_all()
    refreshed = db_session.get(AlertDelivery, delivery.id)
    assert refreshed.attempt_count == 2
    assert refreshed.review_action == 'retry_limit_reached'


@pytest.mark.parametrize('historical_attempt_count', (0, -3))
def test_delivery_normalizes_invalid_attempt_count_before_manual_retry(
    app,
    db_session,
    historical_attempt_count,
):
    from core.db_models import AlertDelivery, User, WeatherAlert
    from core.time_utils import utcnow
    from services.push.dispatch import _claim_delivery

    app.config['WXPUSHER_MAX_DELIVERY_ATTEMPTS'] = 2
    user = User(
        username=f'wx_retry_invalid_{abs(historical_attempt_count)}',
        role='user',
    )
    user.set_password('UserPassword1!')
    db_session.add(user)
    alert = WeatherAlert(
        alert_date=utcnow(),
        location='116.20,29.27',
        alert_type='heat_threshold',
        alert_level='阈值',
        description='test',
        affected_communities='[]',
        disease_correlation='{}',
    )
    db_session.add(alert)
    db_session.flush()
    delivery = AlertDelivery(
        alert_id=alert.id,
        user_id=user.id,
        channel='wxpusher',
        status='retry_ready',
        delivery_token=f'retry-invalid-{abs(historical_attempt_count)}',
        sent_at=utcnow(),
        attempt_count=historical_attempt_count,
        review_action='allow_retry',
        reviewed_at=utcnow(),
        reviewed_by_user_id=user.id,
    )
    db_session.add(delivery)
    db_session.commit()

    claim = _claim_delivery(
        alert_id=alert.id,
        user_id=user.id,
        pair_id=None,
        now=utcnow(),
    )

    assert claim['action'] == 'send'
    db_session.expire_all()
    refreshed = db_session.get(AlertDelivery, delivery.id)
    assert refreshed.status == 'sending'
    assert refreshed.attempt_count == 2


def test_dispatch_requires_verified_uid_ownership(app, db_session):
    from core.db_models import Pair, User
    from core.time_utils import utcnow
    from services.push.dispatch import _reload_push_authorization

    user = User(
        username='wx_unverified_dispatch',
        role='user',
        wxpusher_uid='UID_UNVERIFIED_DISPATCH',
        push_enabled=True,
        wxpusher_consent_version=app.config['WX_MINIPROGRAM_PRIVACY_VERSION'],
        wxpusher_consented_at=utcnow(),
    )
    user.set_password('UserPassword1!')
    db_session.add(user)
    db_session.flush()
    pair = Pair(
        caregiver_id=user.id,
        community_code='都昌',
        location_query='都昌',
        elder_code='wx-unverified-elder',
        short_code='WXUNVERIFIED',
        status='active',
    )
    db_session.add(pair)
    db_session.commit()

    assert _reload_push_authorization(user.id, [pair.id]) is None


def test_mp_api_rejects_direct_uid_replacement(app, client, db_session):
    from core.db_models import User
    from core.time_utils import utcnow
    from core.usage import create_api_token

    app.config.update(
        FEATURE_WXPUSHER=True,
        WXPUSHER_APP_TOKEN='AT_abcdefghijklmnop',
    )
    user = User(
        username='wx_mp_direct_reject',
        role='user',
        wxpusher_uid='UID_EXISTING_OWNER',
        wxpusher_uid_verified_at=utcnow(),
        push_enabled=False,
    )
    user.set_password('UserPassword1!')
    db_session.add(user)
    db_session.commit()
    token = create_api_token(user.id, name='wx-security')

    response = client.patch(
        '/mp/api/v1/me',
        json={'wxpusher_uid': 'UID_UNPROVEN_REPLACEMENT'},
        headers={'Authorization': f'Bearer {token}'},
    )

    assert response.status_code == 409
    assert response.get_json()['error'] == 'wxpusher_verification_required'
    db_session.expire_all()
    refreshed = db_session.get(User, user.id)
    assert refreshed.wxpusher_uid == 'UID_EXISTING_OWNER'
    assert refreshed.wxpusher_uid_verified_at is not None


def test_mp_api_rejects_direct_submission_of_historical_unverified_uid(
    app,
    client,
    db_session,
):
    from core.db_models import User
    from core.usage import create_api_token

    app.config.update(
        FEATURE_WXPUSHER=True,
        WXPUSHER_APP_TOKEN='AT_abcdefghijklmnop',
    )
    user = User(
        username='wx_mp_historical_unverified',
        role='user',
        wxpusher_uid='UID_MP_HISTORICAL',
        push_enabled=False,
    )
    user.set_password('UserPassword1!')
    db_session.add(user)
    db_session.commit()
    token = create_api_token(user.id, name='wx-historical-unverified')

    response = client.patch(
        '/mp/api/v1/me',
        json={'wxpusher_uid': 'UID_MP_HISTORICAL'},
        headers={'Authorization': f'Bearer {token}'},
    )

    assert response.status_code == 409
    assert response.get_json()['error'] == 'wxpusher_verification_required'
    db_session.expire_all()
    refreshed = db_session.get(User, user.id)
    assert refreshed.wxpusher_uid == 'UID_MP_HISTORICAL'
    assert refreshed.wxpusher_uid_verified_at is None
    assert refreshed.push_enabled is False


def test_mp_api_masks_unverified_push_as_disabled(app, client, db_session):
    from core.db_models import User
    from core.time_utils import utcnow
    from core.usage import create_api_token

    app.config.update(
        FEATURE_WXPUSHER=True,
        WXPUSHER_APP_TOKEN='AT_abcdefghijklmnop',
    )
    user = User(
        username='wx_mp_unverified_mask',
        role='user',
        wxpusher_uid='UID_UNVERIFIED_MASK',
        push_enabled=True,
        wxpusher_consent_version=app.config['WX_MINIPROGRAM_PRIVACY_VERSION'],
        wxpusher_consented_at=utcnow(),
    )
    user.set_password('UserPassword1!')
    db_session.add(user)
    db_session.commit()
    token = create_api_token(user.id, name='wx-unverified-mask')

    response = client.get(
        '/mp/api/v1/me',
        headers={'Authorization': f'Bearer {token}'},
    )

    assert response.status_code == 200
    data = response.get_json()['data']
    assert data['wxpusher_uid'] == 'UID_UNVERIFIED_MASK'
    assert data['push_enabled'] is False
    assert data['wxpusher_uid_verified'] is False


def test_web_profile_rejects_direct_uid_replacement(app, client, db_session):
    from core.db_models import User
    from core.time_utils import utcnow

    app.config.update(
        FEATURE_WXPUSHER=True,
        WXPUSHER_APP_TOKEN='AT_abcdefghijklmnop',
    )
    user = User(
        username='wx_web_direct_reject',
        role='user',
        wxpusher_uid='UID_WEB_EXISTING',
        wxpusher_uid_verified_at=utcnow(),
        push_enabled=False,
    )
    user.set_password('UserPassword1!')
    db_session.add(user)
    db_session.commit()
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'wx-web-security-csrf'

    response = client.post(
        '/profile',
        data={
            'form_id': 'basic',
            'email': '',
            'age': '',
            'gender': '',
            'community': '',
            'wxpusher_uid': 'UID_WEB_UNPROVEN',
            'push_enabled': 'on',
            'wxpusher_consent': '1',
            'wxpusher_consent_version': app.config['WX_MINIPROGRAM_PRIVACY_VERSION'],
            'csrf_token': 'wx-web-security-csrf',
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    db_session.expire_all()
    refreshed = db_session.get(User, user.id)
    assert refreshed.wxpusher_uid == 'UID_WEB_EXISTING'
    assert refreshed.wxpusher_uid_verified_at is not None
    assert refreshed.push_enabled is False


def test_web_profile_allows_basic_update_with_historical_unverified_uid(
    app,
    client,
    db_session,
):
    from core.db_models import Community, User

    app.config.update(
        FEATURE_WXPUSHER=True,
        WXPUSHER_APP_TOKEN='AT_abcdefghijklmnop',
    )
    user = User(
        username='wx_web_historical_uid',
        role='user',
        wxpusher_uid='UID_HISTORICAL_UNVERIFIED',
        push_enabled=False,
        age=60,
        community='原社区',
    )
    user.set_password('UserPassword1!')
    db_session.add_all([user, Community(name='新社区')])
    db_session.commit()
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'wx-historical-csrf'

    response = client.post(
        '/profile',
        data={
            'form_id': 'basic',
            'email': '',
            'age': '61',
            'gender': '',
            'community': '新社区',
            'wxpusher_uid': 'UID_HISTORICAL_UNVERIFIED',
            'csrf_token': 'wx-historical-csrf',
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    db_session.expire_all()
    refreshed = db_session.get(User, user.id)
    assert refreshed.age == 61
    assert refreshed.community == '新社区'
    assert refreshed.wxpusher_uid == 'UID_HISTORICAL_UNVERIFIED'
    assert refreshed.wxpusher_uid_verified_at is None
    assert refreshed.push_enabled is False


def test_web_profile_clear_removes_verification_when_feature_is_disabled(
    app,
    client,
    db_session,
):
    from core.db_models import User
    from core.time_utils import utcnow

    app.config.update(
        FEATURE_WXPUSHER=False,
        WXPUSHER_APP_TOKEN='',
    )
    user = User(
        username='wx_web_disabled_clear',
        role='user',
        wxpusher_uid='UID_DISABLED_CLEAR',
        wxpusher_uid_verified_at=utcnow(),
        push_enabled=True,
    )
    user.set_password('UserPassword1!')
    db_session.add(user)
    db_session.commit()
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'wx-disabled-clear-csrf'

    response = client.post(
        '/profile',
        data={
            'form_id': 'basic',
            'email': '',
            'age': '',
            'gender': '',
            'community': '',
            'remove_wxpusher_uid': '1',
            'csrf_token': 'wx-disabled-clear-csrf',
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    db_session.expire_all()
    refreshed = db_session.get(User, user.id)
    assert refreshed.wxpusher_uid is None
    assert refreshed.wxpusher_uid_verified_at is None
    assert refreshed.push_enabled is False


def test_admin_cannot_authorize_second_manual_retry(app, client, db_session):
    from core.db_models import AlertDelivery, User, WeatherAlert
    from core.time_utils import utcnow

    app.config['WXPUSHER_MAX_DELIVERY_ATTEMPTS'] = 2
    admin = User(username='wx_retry_admin', role='admin')
    admin.set_password('AdminPassword1!')
    recipient = User(username='wx_retry_recipient', role='user')
    recipient.set_password('UserPassword1!')
    db_session.add_all([admin, recipient])
    db_session.flush()
    alert = WeatherAlert(
        alert_date=utcnow(),
        location='116.20,29.27',
        alert_type='heat_threshold',
        alert_level='阈值',
        description='test',
        affected_communities='[]',
        disease_correlation='{}',
    )
    db_session.add(alert)
    db_session.flush()
    delivery = AlertDelivery(
        alert_id=alert.id,
        user_id=recipient.id,
        channel='wxpusher',
        status='uncertain',
        delivery_token='admin-second-retry-token',
        sent_at=utcnow(),
        attempt_count=2,
        error='timeout after manual retry',
    )
    db_session.add(delivery)
    db_session.commit()
    with client.session_transaction() as session:
        session['_user_id'] = admin.get_id()
        session['_fresh'] = True
        session['_csrf_token'] = 'wx-retry-admin-csrf'

    response = client.post(
        f'/analysis/pilot/deliveries/{delivery.id}/review',
        data={
            'action': 'allow_retry',
            'days': '30',
            'csrf_token': 'wx-retry-admin-csrf',
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    db_session.expire_all()
    refreshed = db_session.get(AlertDelivery, delivery.id)
    assert refreshed.status == 'uncertain'
    assert refreshed.attempt_count == 2
    assert refreshed.review_action is None
    assert refreshed.reviewed_at is None
