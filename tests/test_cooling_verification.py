# -*- coding: utf-8 -*-
"""PRD-03 避暑资源核验：迁移、公开页、反馈规则、缺口比例与 CLI。"""
import csv
import importlib
import importlib.util
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from core.db_models import CoolingFeedback, CoolingResource, Pair, User
from core.extensions import db
from core.security import hash_short_code
from core.time_utils import today_local, utcnow
from services.cooling_service import (
    compute_verify_status,
    record_feedback,
    record_verification,
    resource_gaps,
)


ROOT = Path(__file__).resolve().parents[1]


def _csrf(client, token='cooling-csrf'):
    with client.session_transaction() as sess:
        sess['_csrf_token'] = token
    return token


def _login_as(client, user_id):
    user = db.session.get(User, user_id)
    assert user is not None
    with client.session_transaction() as session:
        session['_user_id'] = user.get_id()
        session['_fresh'] = True


def _user(username, role='user'):
    user = User(username=username, role=role)
    user.set_password('pass12344')
    db.session.add(user)
    db.session.commit()
    return user


def _pair(user, code, elder_code, community_code='都昌', is_test=False):
    pair = Pair(
        caregiver_id=user.id,
        community_code=community_code,
        location_query=community_code,
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


def _resource(**kwargs):
    payload = {
        'community_code': '都昌',
        'name': '测试纳凉点',
        'resource_type': '图书馆',
        'is_active': True,
    }
    payload.update(kwargs)
    resource = CoolingResource(**payload)
    db.session.add(resource)
    db.session.commit()
    return resource


def _load_cli():
    path = ROOT / 'scripts' / 'cooling_verify.py'
    spec = importlib.util.spec_from_file_location('cooling_verify_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cooling_verification_migration_is_current_head():
    script = ScriptDirectory.from_config(Config('alembic.ini'))
    assert script.get_heads() == ['0018_health_consent_care']


def test_cooling_verification_migration_up_and_down_on_sqlite(monkeypatch):
    migration = importlib.import_module('migrations.versions.0016_cooling_verification')
    engine = sa.create_engine('sqlite:///:memory:')
    metadata = sa.MetaData()
    sa.Table(
        'pairs',
        metadata,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('community_code', sa.String(length=100), nullable=False),
    )
    sa.Table(
        'cooling_resources',
        metadata,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('community_code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('is_active', sa.Boolean()),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, 'op', operations)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column['name']
            for column in sa.inspect(connection).get_columns('cooling_resources')
        }
        tables = set(sa.inspect(connection).get_table_names())
        expected = {
            'last_verified_at',
            'verified_by_role',
            'verify_method',
            'open_during_alert',
            'alert_open_note_code',
            'amenities_json',
            'transport_need',
            'verify_status',
        }
        assert expected <= columns
        assert 'cooling_feedback' in tables

        migration.downgrade()
        columns_after = {
            column['name']
            for column in sa.inspect(connection).get_columns('cooling_resources')
        }
        tables_after = set(sa.inspect(connection).get_table_names())

    assert expected.isdisjoint(columns_after)
    assert 'cooling_feedback' not in tables_after


def test_unverified_cards_are_grey_and_verified_sort_first(client, db_session, monkeypatch):
    monkeypatch.setattr(
        'services.public_service.get_weather_with_cache',
        lambda location: ({'temperature': 27.5, 'is_mock': False, 'data_source': 'QWeather'}, False),
    )
    unverified = _resource(name='未核验亭子', community_code='都昌', resource_type='亭子')
    verified = _resource(name='已核验图书馆', community_code='都昌', resource_type='图书馆')
    record_verification(
        verified,
        'phone',
        'yes',
        'same_hours',
        {'ac': True, 'water': True, 'seats': True, 'toilet': False, 'step_free': None, 'shade': True},
        'walkable',
        'admin',
    )
    assert unverified.id != verified.id

    response = client.get('/cooling?community=都昌')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '资源点信息由志愿者电话或现场核验' in body
    assert body.find('已核验图书馆') < body.find('未核验亭子')
    assert 'yl-cooling-unverified' in body
    assert '未核验' in body
    assert '最后核验：' in body
    assert '电话' in body
    assert 'data-verify-status="unverified"' in body
    assert 'data-verify-status="verified"' in body


def test_two_different_pairs_closed_trigger_closed_reported(db_session):
    user = _user('cooling_closed_user')
    pair_a = _pair(user, '81110001', 'elder-closed-a')
    pair_b = _pair(user, '81110002', 'elder-closed-b')
    resource = _resource(name='关闭点')
    record_verification(
        resource, 'onsite', 'yes', 'same_hours', {'ac': True}, 'walkable', 'admin',
        now=utcnow() - timedelta(days=1),
    )
    record_feedback(resource, 'closed', pair=pair_a, channel='web_shortcode')
    record_feedback(resource, 'closed', pair=pair_a, channel='web_shortcode')
    db.session.refresh(resource)
    assert compute_verify_status(resource, utcnow()) != 'closed_reported'
    record_feedback(resource, 'closed', pair=pair_b, channel='web_shortcode')
    db.session.refresh(resource)
    assert resource.verify_status == 'closed_reported'
    assert compute_verify_status(resource, utcnow()) == 'closed_reported'


def test_same_pair_two_closed_do_not_trigger_closed_reported(db_session):
    user = _user('cooling_same_pair')
    pair = _pair(user, '81110003', 'elder-closed-same')
    resource = _resource(name='同一户关闭')
    record_feedback(resource, 'closed', pair=pair, channel='web_caregiver')
    record_feedback(resource, 'closed', pair=pair, channel='web_caregiver')
    db.session.refresh(resource)
    assert compute_verify_status(resource, utcnow()) != 'closed_reported'


def test_feedback_unauthenticated_is_401_or_302(client, db_session):
    resource = _resource(name='未登录反馈点')
    token = _csrf(client)
    response = client.post(
        f'/cooling/{resource.id}/feedback',
        data={'csrf_token': token, 'code': 'reachable'},
    )
    assert response.status_code in (401, 302)
    json_response = client.post(
        f'/cooling/{resource.id}/feedback',
        json={'csrf_token': token, 'code': 'reachable'},
        headers={'Accept': 'application/json'},
    )
    assert json_response.status_code in (401, 302)
    assert CoolingFeedback.query.count() == 0


def test_feedback_ignores_free_text_and_rejects_missing_code(client, db_session):
    user = _user('cooling_feedback_user')
    pair = _pair(user, '81110004', 'elder-feedback-free')
    resource = _resource(name='封闭码点')
    token = _csrf(client)
    with client.session_transaction() as sess:
        sess['pair_session_id'] = pair.id
        sess['pair_session_code'] = pair.short_code

    extra = client.post(
        f'/cooling/{resource.id}/feedback',
        data={
            'csrf_token': token,
            'code': 'reachable',
            'comment': '这里很凉快请记下我的电话',
            'note': '自由文本',
        },
    )
    assert extra.status_code in (200, 302)
    row = CoolingFeedback.query.filter_by(resource_id=resource.id).one()
    assert row.code == 'reachable'
    assert not hasattr(row, 'comment')
    assert 'comment' not in CoolingFeedback.__table__.columns
    assert 'note' not in CoolingFeedback.__table__.columns

    missing = client.post(
        f'/cooling/{resource.id}/feedback',
        data={'csrf_token': token, 'note': '只有自由文本'},
    )
    assert missing.status_code in (400, 401, 302)
    invalid = client.post(
        f'/cooling/{resource.id}/feedback',
        json={'csrf_token': token, 'code': '这里写了一段话'},
        headers={'Accept': 'application/json'},
    )
    assert invalid.status_code == 400
    assert CoolingFeedback.query.filter_by(resource_id=resource.id).count() == 1


def test_households_ratio_none_without_active_pairs(client, db_session):
    admin = _user('cooling_admin_gaps', role='admin')
    _resource(name='无户资源')
    gaps = resource_gaps(today_local())
    assert gaps['summary']['households_with_one_viable_option_ratio'] is None

    _login_as(client, admin.id)
    response = client.get('/analysis/pilot?days=30')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '资源缺口' in body
    assert '资源可达性审计，不代表公共服务能力' in body
    assert '至少一处可用资源的户比例：--' in body


def test_households_ratio_counts_community_code_and_excludes_test_pairs(db_session):
    user = _user('cooling_ratio_user')
    _pair(user, '81110011', 'elder-ratio-a', community_code='甲村')
    _pair(user, '81110012', 'elder-ratio-b', community_code='乙村')
    _pair(user, '81110013', 'elder-ratio-test', community_code='甲村', is_test=True)
    resource = _resource(name='甲村服务中心', community_code='甲村')
    record_verification(
        resource, 'phone', 'conditional', 'staff_dependent', {'ac': True}, 'bus', 'student',
    )
    gaps = resource_gaps(today_local(), include_test=False)
    assert gaps['summary']['households_with_one_viable_option_ratio'] == 0.5
    assert gaps['summary']['verified_count'] == 1


def test_cli_writes_temp_sqlite_and_ledger(app, db_session, tmp_path, monkeypatch):
    resource = _resource(
        name='CLI 卫生院',
        community_code='周溪',
        resource_type='卫生院',
        contact_hint='私人手机不应入账',
    )
    ledger = tmp_path / 'cooling_verification_ledger.csv'
    ledger.write_text(
        'verified_at,resource_id,resource_type,township,method,open_during_alert,'
        'alert_open_note_code,ac,water,seats,toilet,step_free,shade,transport_need,'
        'result_code,notes_code\n',
        encoding='utf-8',
    )
    cli = _load_cli()
    monkeypatch.setattr(cli, 'LEDGER_PATH', ledger)
    resource_id = resource.id
    cli.run_verify(
        resource_id,
        'phone',
        open_during_alert='yes',
        alert_note='same_hours',
        amenities={
            'ac': True,
            'water': True,
            'seats': True,
            'toilet': False,
            'step_free': None,
            'shade': True,
        },
        transport='bus',
        result='verified',
        note='ok',
    )
    updated = db.session.get(CoolingResource, resource_id)
    assert updated.verify_status == 'verified'
    assert updated.verify_method == 'phone'
    assert updated.verified_by_role == 'student'
    assert updated.transport_need == 'bus'

    text = ledger.read_text(encoding='utf-8')
    rows = list(csv.DictReader(ledger.open(encoding='utf-8')))
    assert len(rows) == 1
    row = rows[0]
    assert row['resource_id'] == str(resource.id)
    assert row['township'] == '周溪'
    assert row['method'] == 'phone'
    assert row['open_during_alert'] == 'yes'
    assert row['ac'] == 'true'
    assert row['toilet'] == 'false'
    assert row['step_free'] == ''
    assert row['result_code'] == 'verified'
    assert row['notes_code'] == 'ok'
    assert '私人手机' not in text
    assert '不应入账' not in text


def test_resource_gaps_csv_and_json_for_admin(client, db_session):
    admin = _user('cooling_gaps_export', role='admin')
    resource = _resource(name='导出点', community_code='都昌')
    _login_as(client, admin.id)

    csv_response = client.get('/analysis/pilot/resource_gaps.csv')
    assert csv_response.status_code == 200
    csv_body = csv_response.get_data(as_text=True)
    assert 'verify_status' in csv_body
    assert 'households_with_one_viable_option_ratio' in csv_body
    assert str(resource.id) in csv_body

    json_response = client.get('/analysis/pilot/resource_gaps_summary.json')
    assert json_response.status_code == 200
    payload = json_response.get_json()
    assert payload['households_with_one_viable_option_ratio'] is None
    assert payload['unverified_count'] >= 1


def test_admin_verify_form_records_verification(client, db_session):
    admin = _user('cooling_admin_verify', role='admin')
    resource = _resource(name='管理端核验点')
    token = _csrf(client, 'admin-verify-csrf')
    _login_as(client, admin.id)
    response = client.post(
        f'/admin/cooling/{resource.id}/verify',
        data={
            'csrf_token': token,
            'method': 'onsite',
            'open_during_alert': 'yes',
            'alert_open_note_code': 'extended',
            'transport_need': 'walkable',
            'amenity_ac': '1',
            'amenity_water': '1',
        },
    )
    assert response.status_code in (200, 302)
    db.session.refresh(resource)
    assert resource.verified_by_role == 'admin'
    assert resource.verify_method == 'onsite'
    assert resource.verify_status == 'verified'
    assert resource.open_during_alert == 'yes'
