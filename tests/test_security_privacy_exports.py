# -*- coding: utf-8 -*-
"""用合成地址验证埋点最小化与电子表格输出边界。"""
import csv
import io
import json
import logging

import pytest


ADDRESS = '测试县虚构路987号8栋203室'


def test_usage_event_never_copies_location(app, db_session):
    from core.usage import log_usage_event

    with app.app_context():
        event = log_usage_event('pair_created', meta={
            'location_query': ADDRESS, 'nested': {'coordinates': '116.12345,29.45678'},
        })
        assert event is not None
        assert event.meta_json is None or ADDRESS not in event.meta_json
        assert not event.meta_json or '116.12345' not in event.meta_json


def test_usage_event_retains_only_valid_aggregate_metadata(app, db_session):
    from core.usage import log_usage_event

    with app.app_context():
        event = log_usage_event('feedback_submitted', meta={
            'optin': True, 'difficulty_len': 12, 'has_note': True,
            'caregiver_actions_count': 2, 'location_query': ADDRESS,
            'nested': {'address': ADDRESS},
        })
        data = json.loads(event.meta_json)
        assert data == {'optin': True, 'difficulty_len': 12,
                        'has_note': True, 'caregiver_actions_count': 2}
        malformed = log_usage_event('feedback_submitted', meta={'difficulty_len': ADDRESS})
        assert malformed.meta_json is None or ADDRESS not in malformed.meta_json


@pytest.mark.parametrize('formula', ['=1+1', '+SUM(1,1)', '-1+1', '@SUM(1,1)',
                                   '\t=1+1', '\r=1+1', '  =1+1', '\ufeff=1+1'])
def test_admin_csv_neutralizes_stored_formula(app, db_session, admin_client, formula):
    from core.db_models import UsageEvent
    from core.time_utils import utcnow

    with app.app_context():
        db_session.add(UsageEvent(event_type='template_copy', source=formula,
                                  meta_json='{"label":"中文,引号\\\"与换行\\n"}',
                                  created_at=utcnow()))
        db_session.commit()
    response = admin_client.get('/analysis/pilot/export.csv')
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
    assert rows[1][5] == "'" + formula
    assert json.loads(rows[1][6])['label'] == '中文,引号"与换行\n'


def test_normal_csv_values_are_preserved(app, db_session, admin_client):
    from core.db_models import UsageEvent
    from core.time_utils import utcnow

    with app.app_context():
        db_session.add(UsageEvent(event_type='template_copy', source='web', created_at=utcnow()))
        db_session.commit()
    response = admin_client.get('/analysis/pilot/export.csv')
    rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
    assert rows[1][1] == 'template_copy'
    assert rows[1][5] == 'web'


def test_web_pair_creation_omits_address_analytics(app, db_session):
    from core.db_models import Pair, UsageEvent, User
    from flask_login import login_user
    from services.user.caregiver_service import _create_pair

    with app.test_request_context('/pairs'):
        user = User(username='privacy-web', password_hash='not-a-login-secret', role='caregiver')
        db_session.add(user)
        db_session.commit()
        login_user(user)
        pair = _create_pair(ADDRESS)
        assert db_session.get(Pair, pair.id).location_query == ADDRESS
        event = UsageEvent.query.filter_by(event_type='pair_created').one()
        assert ADDRESS not in (event.meta_json or '')


def test_web_creation_error_does_not_log_input(app, client, db_session, monkeypatch, caplog):
    from core.db_models import User
    from services.user import caregiver_service

    with app.app_context():
        user = User(username='privacy-failure', password_hash='not-a-login-secret', role='caregiver')
        db_session.add(user)
        db_session.commit()
        uid = user.id
    with client.session_transaction() as session:
        session['_user_id'] = str(uid)
        session['_fresh'] = True
        session['_csrf_token'] = 'privacy-csrf'

    def fail(*args, **kwargs):
        raise RuntimeError('SQL contains ' + ADDRESS)

    monkeypatch.setattr(caregiver_service, '_create_pair', fail)
    with caplog.at_level(logging.DEBUG):
        response = client.post('/caregiver/pair/create', data={
            'location_query': ADDRESS, 'csrf_token': 'privacy-csrf',
        })
    assert response.status_code == 302
    assert ADDRESS not in caplog.text


def test_push_fallback_does_not_log_address(app, db_session, monkeypatch, caplog):
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import utcnow
    from services.push import dispatch

    with app.app_context():
        user = User(username='privacy-push', password_hash='not-a-login-secret', role='user', push_enabled=True, wxpusher_uid='UID_FAKE')
        db_session.add(user)
        db_session.flush()
        db_session.add(Pair(caregiver_id=user.id, community_code='测试社区',
                            location_query=ADDRESS, elder_code='privacy-push-elder',
                            short_code='12348765', short_code_hash=hash_short_code('12348765'),
                            status='active', last_active_at=utcnow()))
        db_session.commit()
        monkeypatch.setattr(dispatch, 'resolve_location', lambda value: {'provider': 'fallback'})
        with caplog.at_level(logging.WARNING):
            dispatch.dispatch_alerts()
        assert ADDRESS not in caplog.text


def test_historical_scrub_preview_apply_and_idempotence(app, db_session):
    from core.db_models import UsageEvent
    from scripts.scrub_usage_metadata import scrub_usage_metadata

    with app.app_context():
        row = UsageEvent(event_type='feedback_submitted', source='web',
                         meta_json=json.dumps({'location_query': ADDRESS, 'optin': True}))
        db_session.add(row)
        db_session.commit()
        assert scrub_usage_metadata(db_session, batch_size=1) == 1
        assert 'location_query' in row.meta_json
        assert scrub_usage_metadata(db_session, apply=True, batch_size=1) == 1
        assert json.loads(row.meta_json) == {'optin': True}
        assert scrub_usage_metadata(db_session, apply=True, batch_size=1) == 0


def test_all_text_csv_columns_are_safe(app, db_session, admin_client):
    from core.db_models import UsageEvent
    from core.time_utils import utcnow

    with app.app_context():
        db_session.add(UsageEvent(event_type='=1+1', source='@SUM(1,1)',
                                  meta_json='\u200b=1+1', created_at=utcnow()))
        db_session.commit()
    response = admin_client.get('/analysis/pilot/export.csv')
    rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
    assert rows[1][1] == "'=1+1"
    assert rows[1][5] == "'@SUM(1,1)"
    assert rows[1][6] == "'\u200b=1+1"


def test_miniprogram_creation_keeps_business_address_private(app, client, db_session):
    from core.db_models import Pair, UsageEvent, User
    from core.usage import create_api_token

    with app.app_context():
        user = User(username='privacy-mp', password_hash='not-a-login-secret', role='caregiver')
        db_session.add(user)
        db_session.commit()
        token = create_api_token(user.id)
    response = client.post('/mp/api/v1/elders', json={
        'name': '合成人物', 'location_query': ADDRESS, 'age': 70,
    }, headers={'Authorization': 'Bearer ' + token})
    assert response.status_code == 200
    with app.app_context():
        assert Pair.query.one().location_query == ADDRESS
        assert all(ADDRESS not in (event.meta_json or '') for event in UsageEvent.query.all())


def test_usage_error_does_not_log_sql_parameters(app, db_session, monkeypatch, caplog):
    from core.usage import log_usage_event
    from core.extensions import db

    def fail():
        raise RuntimeError('query parameters: ' + ADDRESS)

    with app.app_context(), caplog.at_level(logging.DEBUG):
        monkeypatch.setattr(db.session, 'commit', fail)
        assert log_usage_event('pair_created', meta={'location_query': ADDRESS}) is None
    assert ADDRESS not in caplog.text


def test_real_pair_transaction_failure_does_not_log_address(app, db_session, monkeypatch, caplog):
    from core.db_models import User
    from flask_login import login_user
    from services.user.caregiver_service import _create_pair
    from sqlalchemy.exc import IntegrityError

    with app.test_request_context('/pairs'):
        user = User(username='privacy-transaction', password_hash='not-a-login-secret', role='caregiver')
        db_session.add(user)
        db_session.commit()
        login_user(user)
        def fail(*args, **kwargs):
            raise IntegrityError('INSERT INTO pairs VALUES (?)', (ADDRESS,), RuntimeError('synthetic failure'))
        monkeypatch.setattr(db_session, 'flush', fail)
        with caplog.at_level(logging.DEBUG), pytest.raises(IntegrityError):
            _create_pair(ADDRESS)
        assert ADDRESS not in caplog.text
