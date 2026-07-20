# -*- coding: utf-8 -*-
"""Smoke tests for admin routes.

These routes are easy to accidentally break due to DB dialect detection or
Flask-SQLAlchemy/SQLAlchemy version differences.
"""
import csv
from datetime import datetime, timedelta
import io


def _login_as(client, user_id: int):
    with client.session_transaction() as session:
        session['_user_id'] = f'{user_id}:1'
        session['_fresh'] = True


def test_admin_password_reset_revokes_target_sessions(app, client):
    """管理员重设密码时同步撤销目标用户的所有旧凭证。"""
    from core.db_models import (
        ApiToken,
        MiniProgramIdentity,
        MiniProgramSession,
        User,
    )
    from core.extensions import db
    from core.time_utils import utcnow

    with app.app_context():
        db.drop_all()
        db.create_all()
        admin = User(username='admin-password-reset', role='admin')
        admin.set_password('AdminPassword1!')
        target = User(username='admin_reset_target', role='user')
        target.set_password('TargetPassword1!')
        db.session.add_all([admin, target])
        db.session.flush()
        now = utcnow()
        api_token = ApiToken(
            user_id=target.id,
            name='管理员改密前凭证',
            token_hash='e' * 64,
            created_at=now,
            expires_at=now + timedelta(days=30),
            scopes='miniapp:read',
            privacy_consent_version='privacy-v1',
        )
        identity = MiniProgramIdentity(
            user_id=target.id,
            openid_hash='f' * 64,
            privacy_consent_version='privacy-v1',
            privacy_consented_at=now,
            acquisition_source='direct',
            created_at=now,
            last_login_at=now,
        )
        db.session.add_all([api_token, identity])
        db.session.flush()
        mini_session = MiniProgramSession(
            identity_id=identity.id,
            user_id=target.id,
            token_hash='1' * 64,
            privacy_consent_version='privacy-v1',
            expires_at=now + timedelta(days=30),
            created_at=now,
            last_used_at=now,
        )
        db.session.add(mini_session)
        db.session.commit()
        admin_id = int(admin.id)
        target_id = int(target.id)
        target_username = target.username
        target_session_id = target.get_id()
        api_token_id = int(api_token.id)
        mini_session_id = int(mini_session.id)

    target_client = client.application.test_client()
    with target_client.session_transaction() as session:
        session['_user_id'] = target_session_id
        session['_fresh'] = True

    csrf_token = 'admin-password-reset-csrf'
    _login_as(client, admin_id)
    with client.session_transaction() as session:
        session['_csrf_token'] = csrf_token
    response = client.post(
        f'/admin/user/{target_id}/edit',
        data={
            'username': target_username,
            'email': '',
            'age': '',
            'gender': '',
            'community': '',
            'role': 'user',
            'password': 'ResetPassword2!',
            'csrf_token': csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert response.headers['Location'].endswith('/admin/users')
    with app.app_context():
        assert db.session.get(User, target_id).auth_version == 2
        assert db.session.get(User, target_id).check_password('ResetPassword2!')
        assert db.session.get(ApiToken, api_token_id).revoked_at is not None
        assert db.session.get(MiniProgramSession, mini_session_id).revoked_at is not None
    stale_response = target_client.get('/profile', follow_redirects=False)
    assert stale_response.status_code in (301, 302)
    assert '/login' in stale_response.headers['Location']


def test_admin_dashboard_renders(client, db_session):
    from core.db_models import MedicalRecord, User

    admin = User(username='admin_test', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.add_all([
        MedicalRecord(
            patient_name='甲',
            visit_time=datetime(2024, 1, 15, 8, 0),
            disease_category='呼吸系统疾病',
        ),
        MedicalRecord(
            patient_name='乙',
            visit_time=datetime(2024, 3, 20, 8, 0),
            disease_category='心血管疾病',
        ),
    ])
    db_session.commit()

    _login_as(client, admin.id)
    resp = client.get('/admin')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '管理后台仪表板' in body
    assert '数据时间范围：2024-01 至 2024-03（2 个有记录月份）' in body
    assert '当前展示病例数最多的 2 个分类' in body
    assert '2023-12 至 2025-01（共13个月）' not in body
    assert '共48种疾病分类' not in body


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
    assert '暂无可计算数据' in body
    assert '本页尚未接入按同一日期对齐的天气与病例序列' in body
    assert '相关系数分析（基于历史数据）' not in body
    assert 'data: [35, 75, 65, 85, 70]' not in body
    assert 'min="2023-12-01"' not in body


def test_pilot_dashboard_renders_for_admin(client, db_session):
    from core.db_models import MiniProgramIdentity, UsageEvent, User
    from core.time_utils import utcnow

    admin = User(username='admin_pilot', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    direct_user = User(username='pilot_direct_user', role='user')
    direct_user.set_password('testpass')
    shared_user = User(username='pilot_shared_user', role='user')
    shared_user.set_password('testpass')
    recent_user = User(username='pilot_recent_user', role='user')
    recent_user.set_password('testpass')
    db_session.add_all([direct_user, shared_user, recent_user])
    db_session.flush()
    now = utcnow()
    db_session.add_all([
        MiniProgramIdentity(
            user_id=direct_user.id,
            openid_hash='pilot-direct-openid',
            privacy_consent_version='privacy-v1',
            privacy_consented_at=now - timedelta(days=20),
            acquisition_source='direct',
            created_at=now - timedelta(days=20),
        ),
        MiniProgramIdentity(
            user_id=shared_user.id,
            openid_hash='pilot-shared-openid',
            privacy_consent_version='privacy-v1',
            privacy_consented_at=now - timedelta(days=20),
            acquisition_source='family_share',
            created_at=now - timedelta(days=20),
        ),
        MiniProgramIdentity(
            user_id=recent_user.id,
            openid_hash='pilot-recent-openid',
            privacy_consent_version='privacy-v1',
            privacy_consented_at=now - timedelta(days=1),
            acquisition_source='direct',
            created_at=now - timedelta(days=1),
        ),
    ])
    db_session.add_all([
        UsageEvent(user_id=direct_user.id, event_type='wechat_login_success', source='miniprogram', meta_json='{"from":"direct"}', created_at=now - timedelta(days=20)),
        UsageEvent(user_id=direct_user.id, event_type='elder_profile_created', source='miniprogram', created_at=now - timedelta(days=19)),
        UsageEvent(user_id=direct_user.id, event_type='checkin_confirmed', source='miniprogram', meta_json='{"actions_done_count":1}', created_at=now - timedelta(days=18)),
        UsageEvent(user_id=direct_user.id, event_type='checkin_confirmed', source='miniprogram', meta_json='{"actions_done_count":1}', created_at=now - timedelta(days=9)),
        UsageEvent(user_id=shared_user.id, event_type='wechat_login_success', source='miniprogram', meta_json='{"from":"family_share"}', created_at=now - timedelta(days=20)),
        UsageEvent(user_id=shared_user.id, event_type='elder_profile_created', source='miniprogram', created_at=now - timedelta(days=19)),
        UsageEvent(user_id=shared_user.id, event_type='checkin_confirmed', source='miniprogram', meta_json='{"actions_done_count":1}', created_at=now - timedelta(days=18)),
        UsageEvent(user_id=recent_user.id, event_type='wechat_login_success', source='miniprogram', meta_json='{"from":"direct"}', created_at=now - timedelta(days=1)),
    ])
    db_session.commit()

    _login_as(client, admin.id)
    response = client.get('/analysis/pilot?days=30')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '试点数据看板' in body
    assert '微信小程序有效照护漏斗' in body
    assert '家庭分享来源登录后有效启用率' in body
    assert '直接访问来源登录后有效启用率' in body
    assert '不代表分享卡打开率、分享落地人数或落地到登录转化率' in body
    assert '第二周行动留存' in body


def test_pilot_dashboard_excludes_configured_test_users(client, db_session):
    from core.db_models import MiniProgramIdentity, UsageEvent, User
    from core.time_utils import utcnow

    admin = User(username='admin_pilot_exclusion', role='admin')
    admin.set_password('testpass')
    test_user = User(username='pilot_configured_test_user', role='user')
    test_user.set_password('testpass')
    db_session.add_all([admin, test_user])
    db_session.flush()
    now = utcnow()
    db_session.add(
        MiniProgramIdentity(
            user_id=test_user.id,
            openid_hash='pilot-excluded-openid',
            privacy_consent_version='privacy-v1',
            privacy_consented_at=now - timedelta(days=20),
            acquisition_source='family_share',
            created_at=now - timedelta(days=20),
        )
    )
    db_session.add_all([
        UsageEvent(
            user_id=test_user.id,
            event_type='wechat_login_success',
            source='miniprogram',
            meta_json='{"from":"family_share"}',
            created_at=now - timedelta(days=20),
        ),
        UsageEvent(
            user_id=test_user.id,
            event_type='elder_profile_created',
            source='miniprogram',
            created_at=now - timedelta(days=19),
        ),
        UsageEvent(
            user_id=test_user.id,
            event_type='checkin_confirmed',
            source='miniprogram',
            meta_json='{"actions_done_count":1}',
            created_at=now - timedelta(days=18),
        ),
    ])
    db_session.commit()

    client.application.config['ANALYTICS_TEST_USER_IDS'] = f'999, {test_user.id}'
    _login_as(client, admin.id)
    response = client.get('/analysis/pilot?days=30')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '已启用，配置 2 个，本窗口排除 1 个' in ' '.join(body.split())
    assert '来源未知占比：0.0%' in ' '.join(body.split())
    assert '第二周行动留存：<strong>0 / 0</strong>' in body


def test_pilot_dashboard_suppresses_private_and_small_location_groups(
    client,
    db_session,
):
    """地区卡片只展示达到隐私阈值的社区编码，并排除测试账号。"""
    from core.db_models import Pair, User

    admin = User(username='admin_pilot_location_privacy', role='admin')
    admin.set_password('testpass')
    users = []
    for index in range(6):
        user = User(username=f'pilot_location_user_{index}', role='user')
        user.set_password('testpass')
        users.append(user)
    db_session.add_all([admin, *users])
    db_session.flush()

    pairs = []
    for index, user in enumerate(users[:3]):
        pairs.append(Pair(
            caregiver_id=user.id,
            community_code='safe-community',
            location_query=f'private-address-{index}',
            elder_code=f'location-elder-safe-{index}',
            short_code=f'LOCSAFE{index}',
            status='active',
        ))
    for index, user in enumerate(users[3:5]):
        pairs.append(Pair(
            caregiver_id=user.id,
            community_code='small-community',
            location_query=f'small-private-address-{index}',
            elder_code=f'location-elder-small-{index}',
            short_code=f'LOCSMAL{index}',
            status='active',
        ))
    pairs.append(Pair(
        caregiver_id=users[5].id,
        community_code='test-only-community',
        location_query='test-private-address',
        elder_code='location-elder-test',
        short_code='LOCTEST0',
        status='active',
    ))
    db_session.add_all(pairs)
    db_session.commit()

    client.application.config['ANALYTICS_TEST_USER_IDS'] = str(users[5].id)
    client.application.config['ANALYTICS_MIN_LOCATION_COUNT'] = 3
    _login_as(client, admin.id)
    response = client.get('/analysis/pilot?days=30')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'safe-community' in body
    assert 'private-address' not in body
    assert 'small-community' not in body
    assert 'test-only-community' not in body
    assert '至少 3 个家庭的社区编码' in ' '.join(body.split())


def test_pilot_dashboard_separates_uncertain_clicks_and_reviews_delivery(
    client,
    db_session,
):
    from core.db_models import AlertDelivery, User, WeatherAlert
    from core.time_utils import utcnow

    admin = User(username='admin_delivery_review', role='admin')
    admin.set_password('testpass')
    recipient = User(username='delivery_review_recipient', role='user')
    recipient.set_password('testpass')
    db_session.add_all([admin, recipient])
    db_session.flush()
    now = utcnow()
    alert = WeatherAlert(
        alert_date=now,
        location='116.20,29.27',
        alert_type='heat_threshold',
        alert_level='阈值',
        description='test',
        affected_communities='[]',
        disease_correlation='{}',
    )
    db_session.add(alert)
    db_session.flush()
    sent = AlertDelivery(
        alert_id=alert.id,
        user_id=recipient.id,
        channel='wxpusher',
        status='sent',
        delivery_token='admin-sent-click',
        sent_at=now,
        clicked_at=now,
    )
    uncertain = AlertDelivery(
        alert_id=alert.id,
        user_id=admin.id,
        channel='wxpusher',
        status='uncertain',
        delivery_token='admin-uncertain-click',
        sent_at=now,
        clicked_at=now,
        error='timeout',
    )
    db_session.add_all([sent, uncertain])
    db_session.commit()

    _login_as(client, admin.id)
    response = client.get('/analysis/pilot?days=30')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '推送人工复核队列' in body
    assert '另有 1 次不明确投递主动确认待复核' in body
    assert '100.0%' in body

    with client.session_transaction() as session:
        session['_csrf_token'] = 'delivery-review-csrf'
    reviewed = client.post(
        f'/analysis/pilot/deliveries/{uncertain.id}/review',
        data={
            'action': 'confirm_sent',
            'days': '30',
            'csrf_token': 'delivery-review-csrf',
        },
        follow_redirects=False,
    )

    assert reviewed.status_code in (301, 302)
    db_session.expire_all()
    refreshed = db_session.get(AlertDelivery, uncertain.id)
    assert refreshed.status == 'sent'
    assert refreshed.review_action == 'confirm_sent'
    assert refreshed.reviewed_by_user_id == admin.id
    assert refreshed.reviewed_at is not None


def test_miniprogram_metrics_use_mature_natural_day_cohorts():
    from types import SimpleNamespace

    from core.time_utils import utcnow
    from services.miniprogram_metrics import compute_miniprogram_metrics

    now = utcnow()
    event_id = 0

    def event(user_id, event_type, days_ago, meta_json=''):
        nonlocal event_id
        event_id += 1
        return SimpleNamespace(
            id=event_id,
            user_id=user_id,
            event_type=event_type,
            meta_json=meta_json,
            created_at=now - timedelta(days=days_ago),
        )

    def cohort(user_id, days_ago, acquisition_source='direct'):
        return SimpleNamespace(
            user_id=user_id,
            created_at=now - timedelta(days=days_ago),
            acquisition_source=acquisition_source,
        )

    cohorts = [
        cohort(1, 20),
        cohort(2, 20, 'family_share'),
        cohort(3, 1),
        cohort(4, 20),
    ]

    events = [
        event(1, 'elder_profile_created', 19),
        event(1, 'checkin_confirmed', 18, '{"actions_done_count":1}'),
        event(1, 'checkin_confirmed', 9, '{"actions_done_count":1}'),
        event(2, 'elder_profile_created', 19),
        event(2, 'checkin_confirmed', 18, '{"actions_done_count":1}'),
        event(4, 'elder_profile_created', 19),
        event(4, 'checkin_confirmed', 18, '{"actions_done_count":0}'),
    ]

    result = compute_miniprogram_metrics(events, cohorts=cohorts, as_of=now)

    expected_totals = {
        'cohort_login_users': 4,
        'activation_eligible_users': 3,
        'profile_created_users': 3,
        'activated_users': 2,
        'profile_creation_rate': 1.0,
        'activation_rate': 0.6667,
        'retention_eligible_users': 2,
        'retained_users': 1,
        'week2_retention_rate': 0.5,
        'direct_login_users': 3,
        'direct_activation_eligible_users': 2,
        'direct_activated_users': 1,
        'direct_activation_rate': 0.5,
        'family_share_login_users': 1,
        'family_share_activation_eligible_users': 1,
        'family_share_activated_users': 1,
        'family_share_activation_rate': 1.0,
        'unknown_source_login_users': 0,
        'unknown_source_activation_eligible_users': 0,
        'unknown_source_activated_users': 0,
        'unknown_source_share': 0.0,
        'test_account_exclusion_enabled': False,
        'configured_test_account_ids': 0,
        'excluded_test_account_users': 0,
    }
    for key, expected in expected_totals.items():
        assert result[key] == expected

    assert result['source_breakdown'] == [
        {
            'source': 'direct',
            'label': '直接访问',
            'login_users': 3,
            'd7_mature_users': 2,
            'activated_users': 1,
            'activation_rate': 0.5,
            'd15_mature_users': 1,
            'retained_users': 1,
            'retention_rate': 1.0,
        },
        {
            'source': 'family_share',
            'label': '家庭分享',
            'login_users': 1,
            'd7_mature_users': 1,
            'activated_users': 1,
            'activation_rate': 1.0,
            'd15_mature_users': 1,
            'retained_users': 0,
            'retention_rate': 0.0,
        },
        {
            'source': 'unknown',
            'label': '来源未知',
            'login_users': 0,
            'd7_mature_users': 0,
            'activated_users': 0,
            'activation_rate': 0.0,
            'd15_mature_users': 0,
            'retained_users': 0,
            'retention_rate': 0.0,
        },
    ]
    assert sum(row['activated_users'] for row in result['weekly_action_cohorts']) == 2
    assert sum(row['d15_mature_users'] for row in result['weekly_action_cohorts']) == 2
    assert sum(row['retained_users'] for row in result['weekly_action_cohorts']) == 1


def test_miniprogram_metrics_stratify_week_source_maturity_and_test_exclusion():
    from datetime import timezone
    from types import SimpleNamespace

    from services.miniprogram_metrics import compute_miniprogram_metrics

    now = datetime(2026, 7, 18, 4, 0, tzinfo=timezone.utc)

    def cohort(user_id, days_ago, source):
        return SimpleNamespace(
            id=user_id,
            user_id=user_id,
            created_at=now - timedelta(days=days_ago),
            acquisition_source=source,
        )

    def event(event_id, user_id, event_type, days_ago, meta_json=''):
        return SimpleNamespace(
            id=event_id,
            user_id=user_id,
            event_type=event_type,
            created_at=now - timedelta(days=days_ago),
            meta_json=meta_json,
        )

    cohorts = [
        cohort(1, 20, 'direct'),
        cohort(2, 20, 'family_share'),
        cohort(3, 20, 'legacy-cleared'),
        cohort(4, 20, 'family_share'),
        cohort(5, 3, 'direct'),
    ]
    events = []
    event_id = 0
    for user_id in (1, 2, 3, 4):
        event_id += 1
        events.append(event(event_id, user_id, 'elder_profile_created', 19))
        event_id += 1
        events.append(event(
            event_id,
            user_id,
            'checkin_confirmed',
            18,
            '{"actions_done_count":1}',
        ))
    for user_id in (1, 3, 4):
        event_id += 1
        events.append(event(
            event_id,
            user_id,
            'checkin_confirmed',
            9,
            '{"actions_done_count":1}',
        ))

    result = compute_miniprogram_metrics(
        events,
        cohorts=cohorts,
        as_of=now,
        excluded_user_ids={4, 999},
    )

    assert result['cohort_login_users'] == 4
    assert result['activation_eligible_users'] == 3
    assert result['retention_eligible_users'] == 3
    assert result['retained_users'] == 2
    assert result['unknown_source_share'] == 0.25
    assert result['test_account_exclusion_enabled'] is True
    assert result['configured_test_account_ids'] == 2
    assert result['excluded_test_account_users'] == 1

    by_source = {row['source']: row for row in result['source_breakdown']}
    assert by_source['direct'] == {
        'source': 'direct',
        'label': '直接访问',
        'login_users': 2,
        'd7_mature_users': 1,
        'activated_users': 1,
        'activation_rate': 1.0,
        'd15_mature_users': 1,
        'retained_users': 1,
        'retention_rate': 1.0,
    }
    assert by_source['family_share']['d7_mature_users'] == 1
    assert by_source['family_share']['d15_mature_users'] == 1
    assert by_source['family_share']['retained_users'] == 0
    assert by_source['unknown']['d7_mature_users'] == 1
    assert by_source['unknown']['d15_mature_users'] == 1
    assert by_source['unknown']['retained_users'] == 1

    assert len(result['weekly_action_cohorts']) == 1
    weekly = result['weekly_action_cohorts'][0]
    assert weekly['activated_users'] == 3
    assert weekly['d15_mature_users'] == 3
    assert weekly['retained_users'] == 2
    assert weekly['retention_rate'] == 0.6667
    assert weekly['sources']['direct']['activated_users'] == 1
    assert weekly['sources']['family_share']['activated_users'] == 1
    assert weekly['sources']['unknown']['activated_users'] == 1


def test_miniprogram_metrics_exclude_d7_and_mature_retention_on_d15():
    from types import SimpleNamespace

    from core.time_utils import utcnow
    from services.miniprogram_metrics import compute_miniprogram_metrics

    now = utcnow()

    def record(user_id, event_type, days_ago, meta_json=''):
        return SimpleNamespace(
            id=user_id * 10 + days_ago,
            user_id=user_id,
            event_type=event_type,
            meta_json=meta_json,
            created_at=now - timedelta(days=days_ago),
        )

    cohorts = [
        SimpleNamespace(user_id=1, created_at=now - timedelta(days=20), acquisition_source='direct'),
        SimpleNamespace(user_id=2, created_at=now - timedelta(days=20), acquisition_source='direct'),
    ]
    events = [
        # 用户 1 在 D6 完成启用，首次行动距今 14 天，D8-D14 尚未完整成熟。
        record(1, 'elder_profile_created', 14),
        record(1, 'checkin_confirmed', 14, '{"actions_done_count":1}'),
        record(1, 'checkin_confirmed', 6, '{"actions_done_count":1}'),
        # 用户 2 只在 D7 完成，必须排除在 D0-D6 启用窗口外。
        record(2, 'elder_profile_created', 13),
        record(2, 'checkin_confirmed', 13, '{"actions_done_count":1}'),
    ]

    result = compute_miniprogram_metrics(events, cohorts=cohorts, as_of=now)

    assert result['activated_users'] == 1
    assert result['retention_eligible_users'] == 0
    assert result['retained_users'] == 0


def test_miniprogram_activation_cohort_matures_at_local_d7():
    from types import SimpleNamespace

    from core.time_utils import utcnow
    from services.miniprogram_metrics import compute_miniprogram_metrics

    now = utcnow()
    cohorts = [
        SimpleNamespace(user_id=1, created_at=now - timedelta(days=6), acquisition_source='direct'),
        SimpleNamespace(user_id=2, created_at=now - timedelta(days=7), acquisition_source='family_share'),
    ]

    result = compute_miniprogram_metrics([], cohorts=cohorts, as_of=now)

    assert result['activation_eligible_users'] == 1
    assert result['direct_activation_eligible_users'] == 0
    assert result['family_share_activation_eligible_users'] == 1


def test_loaded_miniprogram_metrics_use_identity_and_ignore_web_events(
    db_session,
):
    from core.db_models import MiniProgramIdentity, UsageEvent, User
    from core.time_utils import utcnow
    from services.miniprogram_metrics import load_miniprogram_metrics

    now = utcnow()
    direct_user = User(username='metric-direct-identity', role='user')
    family_user = User(username='metric-family-identity', role='user')
    direct_user.set_password('testpass')
    family_user.set_password('testpass')
    db_session.add_all([direct_user, family_user])
    db_session.flush()
    db_session.add_all([
        MiniProgramIdentity(
            user_id=direct_user.id,
            openid_hash='metric-direct-openid',
            privacy_consent_version='privacy-v1',
            privacy_consented_at=now - timedelta(days=20),
            acquisition_source='direct',
            created_at=now - timedelta(days=20),
        ),
        MiniProgramIdentity(
            user_id=family_user.id,
            openid_hash='metric-family-openid',
            privacy_consent_version='privacy-v1',
            privacy_consented_at=now - timedelta(days=20),
            acquisition_source='family_share',
            created_at=now - timedelta(days=20),
        ),
    ])
    db_session.add_all([
        UsageEvent(
            user_id=direct_user.id,
            event_type='elder_profile_created',
            source='miniprogram',
            created_at=now - timedelta(days=19),
        ),
        UsageEvent(
            user_id=direct_user.id,
            event_type='checkin_confirmed',
            source='miniprogram',
            meta_json='{"actions_done_count":1}',
            created_at=now - timedelta(days=18),
        ),
        UsageEvent(
            user_id=family_user.id,
            event_type='elder_profile_created',
            source='web',
            created_at=now - timedelta(days=19),
        ),
        UsageEvent(
            user_id=family_user.id,
            event_type='checkin_confirmed',
            source='web',
            meta_json='{"actions_done_count":1}',
            created_at=now - timedelta(days=18),
        ),
    ])
    db_session.commit()

    result = load_miniprogram_metrics(now - timedelta(days=30), as_of=now)

    assert result['cohort_login_users'] == 2
    assert result['direct_activated_users'] == 1
    assert result['family_share_activation_eligible_users'] == 1
    assert result['family_share_activated_users'] == 0
    assert result['family_share_activation_rate'] == 0.0


def test_pilot_export_csv_returns_empty_export_for_admin(client, db_session):
    from core.db_models import User

    admin = User(username='admin_pilot_export', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.commit()

    _login_as(client, admin.id)
    response = client.get('/analysis/pilot/export.csv?days=30')

    assert response.status_code == 200
    assert response.mimetype == 'text/csv'
    assert 'pilot_events_last_30d.csv' in response.headers['Content-Disposition']
    assert response.get_data(as_text=True).startswith(
        '\ufefflocal_date,event_type,source,event_count'
    )
    assert 'user_id' not in response.get_data(as_text=True).splitlines()[0]
    assert 'meta_json' not in response.get_data(as_text=True).splitlines()[0]


def test_pilot_export_csv_aggregates_by_local_date_type_and_source_and_excludes_tests(
    client,
    db_session,
):
    from core.db_models import UsageEvent, User
    from core.time_utils import utc_to_local_date, utcnow

    admin = User(username='admin_pilot_aggregated_export', role='admin')
    admin.set_password('testpass')
    ordinary = User(username='pilot_export_ordinary', role='user')
    ordinary.set_password('testpass')
    test_user = User(username='pilot_export_test', role='user')
    test_user.set_password('testpass')
    db_session.add_all([admin, ordinary, test_user])
    db_session.flush()
    now = utcnow()
    event_time = now - timedelta(hours=12)
    db_session.add_all([
        UsageEvent(
            user_id=ordinary.id,
            event_type='template_copy',
            source='miniprogram',
            meta_json='{"channel":"wechat_miniprogram"}',
            created_at=event_time,
        ),
        UsageEvent(
            user_id=ordinary.id,
            event_type='template_copy',
            source='miniprogram',
            meta_json='{"channel":"wechat_miniprogram"}',
            created_at=event_time,
        ),
        UsageEvent(
            user_id=None,
            event_type='template_copy',
            source='web',
            meta_json='{"channel":"web"}',
            created_at=event_time,
        ),
        UsageEvent(
            user_id=test_user.id,
            event_type='template_copy',
            source='miniprogram',
            meta_json='{"channel":"wechat_miniprogram"}',
            created_at=event_time,
        ),
    ])
    db_session.commit()

    client.application.config['ANALYTICS_TEST_USER_IDS'] = str(test_user.id)
    _login_as(client, admin.id)
    response = client.get('/analysis/pilot/export.csv?days=30')

    rows = list(csv.DictReader(io.StringIO(
        response.get_data().decode('utf-8-sig')
    )))
    local_date = utc_to_local_date(event_time).isoformat()
    assert rows == [
        {
            'local_date': local_date,
            'event_type': 'template_copy',
            'source': 'miniprogram',
            'event_count': '2',
        },
        {
            'local_date': local_date,
            'event_type': 'template_copy',
            'source': 'web',
            'event_count': '1',
        },
    ]
    serialized = response.get_data(as_text=True)
    assert 'created_at' not in serialized
    assert 'meta_json' not in serialized
    assert 'wechat_miniprogram' not in serialized


def test_pilot_dashboard_and_export_cap_raw_event_window_at_30_days(
    client,
    db_session,
):
    from core.db_models import User

    admin = User(username='admin_pilot_window_cap', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.commit()

    _login_as(client, admin.id)
    dashboard = client.get('/analysis/pilot?days=90')
    exported = client.get('/analysis/pilot/export.csv?days=365')

    assert dashboard.status_code == 200
    assert '统计口径：最近 30 天' in dashboard.get_data(as_text=True)
    assert '>90天<' not in dashboard.get_data(as_text=True)
    assert 'pilot_events_last_30d.csv' in exported.headers['Content-Disposition']
