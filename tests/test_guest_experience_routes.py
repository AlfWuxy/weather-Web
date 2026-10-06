# -*- coding: utf-8 -*-
"""通过真实登录态、CSRF 和表单验证游客体验的隔离边界。"""
import itertools
import json
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
import sqlalchemy as sa
from flask.testing import FlaskClient


_CLIENT_IPS = itertools.count(10)
_PAGES = ('/experience/', '/experience/profile', '/experience/family',
          '/experience/checkin', '/experience/diary', '/experience/medications')


class _Page(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.links = []
        self.forms = []
        self.inputs = []
        self.form = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.links.append(attrs.get('href', ''))
        elif tag == 'form':
            self.form = {'attrs': attrs, 'inputs': []}
            self.forms.append(self.form)
        elif tag == 'input':
            self.inputs.append(attrs)
            if self.form is not None:
                self.form['inputs'].append(attrs)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.form = None


class _RequestIsolatedClient(FlaskClient):
    def open(self, *args, **kwargs):
        # db_session夹具持有外层app_context；真实WSGI请求应各有独立g与登录身份。
        with self.application.app_context():
            return super().open(*args, **kwargs)


@pytest.fixture
def experience_app(app, db_session, monkeypatch):
    app.config.update(WECHAT_FORMAL_RUNTIME=False, WEB_PRIVATE_FEATURES_ENABLED=True)

    def no_external_requests(*_args, **_kwargs):
        pytest.fail('游客体验集成测试不得请求外部服务')

    monkeypatch.setattr('requests.sessions.Session.request', no_external_requests)
    return app


def _client(app):
    client = _RequestIsolatedClient(app, app.response_class)
    # 游客按IP限流；不同访客使用独立地址，保留真实限流与CSRF中间件。
    client.environ_base['REMOTE_ADDR'] = f'198.51.100.{next(_CLIENT_IPS)}'
    return client


def _guest(app):
    client = _client(app)
    response = client.get('/guest', follow_redirects=False)
    assert response.status_code == 302
    assert urlsplit(response.headers['Location']).path == '/dashboard'
    return client


def _state(app, client):
    from services.guest_experience import get_experience

    with client.session_transaction() as session:
        guest_id = session['_user_id']
    with app.app_context():
        return get_experience(guest_id, create=False)


def _tokens(client, path, operation):
    response = client.get(path)
    assert response.status_code == 200
    for form in _Page(response.get_data(as_text=True)).forms:
        fields = {item.get('name'): item.get('value', '') for item in form['inputs']}
        if fields.get('operation') == operation:
            assert form['attrs'].get('method', '').lower() == 'post'
            assert urlsplit(form['attrs']['action']).path == path
            return {name: fields[name] for name in ('csrf_token', 'version', 'operation')}
    pytest.fail(f'页面缺少可提交的体验表单：{operation}')


def _post(client, path, operation, **payload):
    data = _tokens(client, path, operation)
    data.update(payload)
    return client.post(path, data=data, follow_redirects=False)


def _saved(response, path):
    assert response.status_code == 303, response.get_data(as_text=True)
    assert urlsplit(response.headers['Location']).path == path
    assert response.cache_control.no_store


def _business_rows(session):
    from core.db_models import (AlertDelivery, ApiToken, DailyStatus, Debrief, FamilyMember,
                                HealthDiary, HealthRiskAssessment, MedicationReminder,
                                Pair, UsageEvent, User)

    result = {}
    for model in (User, FamilyMember, HealthDiary, MedicationReminder, HealthRiskAssessment,
                  Pair, DailyStatus, Debrief, ApiToken, UsageEvent, AlertDelivery):
        table = model.__table__
        result[table.name] = [tuple(row) for row in session.execute(sa.select(table).order_by(*table.primary_key))]
    return result


def test_guest_pages_expose_real_forms_navigation_and_private_headers(experience_app):
    client = _guest(experience_app)
    state = _state(experience_app, client)
    assert state['profile']['age'] == 65
    for path in _PAGES:
        response = client.get(path)
        assert response.status_code == 200
        assert response.cache_control.no_store and response.cache_control.private
        assert 'Cookie' in response.vary
        body = response.get_data(as_text=True)
        page = _Page(body)
        assert set(_PAGES).issubset({urlsplit(link).path for link in page.links})
        assert '不会发送真实提醒' in body
        assert page.forms
        for form in page.forms:
            if form['attrs'].get('method', '').lower() != 'post':
                continue
            if not form['attrs'].get('action', '').startswith('/experience/'):
                continue
            fields = {item.get('name'): item.get('value') for item in form['inputs']}
            assert fields['csrf_token']
            assert int(fields['version']) == state['version']
            assert fields['operation']


def test_guest_profile_stays_server_side_and_another_guest_cannot_read_it(experience_app):
    first, second = _guest(experience_app), _guest(experience_app)
    before_second = _state(experience_app, second)
    response = _post(first, '/experience/profile', 'profile', username='隐私示例昵称甲', age='79',
                     gender='女', community='示例社区甲', has_chronic_disease='1',
                     chronic_diseases=['高血压'])
    _saved(response, '/experience/profile')
    after = _state(experience_app, first)
    assert after['profile']['age'] == 79
    assert after['profile']['chronic_diseases'] == ['高血压']
    assert _state(experience_app, second) == before_second
    assert '隐私示例昵称甲' not in second.get('/experience/profile').get_data(as_text=True)
    cookie = first.get_cookie(experience_app.config['SESSION_COOKIE_NAME'])
    decoded = experience_app.session_interface.get_signing_serializer(experience_app).loads(cookie.value)
    assert 'guest_profile' not in decoded and 'guest_assessment' not in decoded
    serialized = json.dumps(decoded, ensure_ascii=False)
    for sensitive in ('隐私示例昵称甲', '示例社区甲', '高血压'):
        assert sensitive not in serialized


def test_guest_forms_require_valid_csrf_without_changing_state(experience_app):
    client = _guest(experience_app)
    state = _state(experience_app, client)
    data = _tokens(client, '/experience/profile', 'profile')
    data.update(username='不会保存的昵称', age='80', gender='未知', community='示例社区')
    data.pop('csrf_token')
    assert client.post('/experience/profile', data=data).status_code == 400
    data['csrf_token'] = 'wrong-token'
    assert client.post('/experience/profile', data=data).status_code == 400
    assert _state(experience_app, client) == state


def test_guest_oversized_form_is_rejected_before_state_mutation(experience_app):
    client = _guest(experience_app)
    state = _state(experience_app, client)
    data = _tokens(client, '/experience/profile', 'profile')
    data.update(username='x' * (17 * 1024), age='80', gender='未知', community='示例社区')
    response = client.post('/experience/profile', data=data)
    assert response.status_code == 413
    assert response.cache_control.no_store
    assert _state(experience_app, client) == state


def test_stale_browser_form_gets_409_without_overwriting_latest_profile(experience_app):
    client = _guest(experience_app)
    stale = _tokens(client, '/experience/profile', 'profile')
    _saved(_post(client, '/experience/profile', 'profile', username='新标签页', age='74',
                 gender='未知', community='示例社区'), '/experience/profile')
    latest = _state(experience_app, client)
    stale.update(username='旧标签页', age='33', gender='未知', community='示例社区')
    response = client.post('/experience/profile', data=stale)
    assert response.status_code == 409
    assert response.cache_control.no_store
    assert _state(experience_app, client) == latest
    assert '这次操作未完成' in response.get_data(as_text=True)


@pytest.mark.parametrize('target', ['foreign', 'missing'])
def test_guest_cannot_reference_another_guests_or_unknown_member(experience_app, target):
    first, second = _guest(experience_app), _guest(experience_app)
    own_state, other_state = _state(experience_app, first), _state(experience_app, second)
    member_id = other_state['members'][0]['id'] if target == 'foreign' else 'demo_member_missing1234'
    response = _post(first, '/experience/checkin', 'checkin', member_id=member_id, actions=['drink_water'])
    assert response.status_code == 404
    assert _state(experience_app, first) == own_state
    assert _state(experience_app, second) == other_state


def test_guest_crud_and_review_do_not_write_business_tables(experience_app, db_session):
    from core.db_models import FamilyMember, User

    owner = User(username='permanent-account', role='user')
    owner.set_password('integration-only-password')
    db_session.add(owner)
    db_session.flush()
    db_session.add(FamilyMember(user_id=owner.id, name='正式档案哨兵', age=82))
    db_session.commit()
    before = _business_rows(db_session)
    client = _guest(experience_app)
    _saved(_post(client, '/experience/family', 'member_save', name='新增示例家人', age='71',
                 relationship='家人'), '/experience/family')
    member = next(item for item in _state(experience_app, client)['members'] if item['name'] == '新增示例家人')
    _saved(_post(client, '/experience/family', 'member_save', id=member['id'], name='修改示例家人', age='73',
                 relationship='长辈'), '/experience/family')
    _saved(_post(client, '/experience/checkin', 'checkin', member_id=member['id'],
                 actions=['drink_water', 'contact']), '/experience/checkin')
    assert not _state(experience_app, client)['checkins'][member['id']]['reviewed']
    _saved(_post(client, '/experience/checkin', 'review', member_id=member['id']), '/experience/checkin')
    assert _state(experience_app, client)['checkins'][member['id']]['reviewed']
    _saved(_post(client, '/experience/diary', 'diary_save', member_id=member['id'], date='2026-10-04',
                 symptoms='合成疲倦记录', severity='轻微', notes='示例记录内容'), '/experience/diary')
    diary = _state(experience_app, client)['diaries'][0]
    _saved(_post(client, '/experience/diary', 'diary_save', id=diary['id'], member_id=member['id'],
                 date='2026-10-04', symptoms='修改合成记录', severity='中等', notes='修改后的示例'), '/experience/diary')
    _saved(_post(client, '/experience/medications', 'medication_save', member_id=member['id'], name='示例药品A',
                 time='08:00', note='仅用于演示'), '/experience/medications')
    medication = _state(experience_app, client)['medications'][0]
    cookie = client.get_cookie(experience_app.config['SESSION_COOKIE_NAME'])
    decoded = experience_app.session_interface.get_signing_serializer(experience_app).loads(cookie.value)
    serialized = json.dumps(decoded, ensure_ascii=False)
    for sensitive in ('修改示例家人', '修改合成记录', '示例药品A', '仅用于演示'):
        assert sensitive not in serialized
    _saved(_post(client, '/experience/medications', 'medication_save', id=medication['id'], member_id=member['id'],
                 name='示例药品B', time='09:00', note='修改后的示例'), '/experience/medications')
    assert _state(experience_app, client)['medications'][0]['time'] == '09:00'
    _saved(_post(client, '/experience/diary', 'diary_delete', id=diary['id']), '/experience/diary')
    _saved(_post(client, '/experience/medications', 'medication_delete', id=medication['id']), '/experience/medications')
    _saved(_post(client, '/experience/family', 'member_delete', id=member['id']), '/experience/family')
    remaining = _state(experience_app, client)
    assert member['id'] not in {item['id'] for item in remaining['members']}
    assert member['id'] not in remaining['checkins']
    assert remaining['diaries'] == [] and remaining['medications'] == []
    _saved(_post(client, '/experience/', 'reset'), '/experience/')
    assert _state(experience_app, client)['profile']['age'] == 65
    assert _business_rows(db_session) == before


@pytest.mark.parametrize('path', ['/family-members', '/health-diary', '/medication-reminders', '/pairs'])
def test_guest_experience_does_not_unlock_real_record_routes(experience_app, db_session, path):
    client = _guest(experience_app)
    before = _business_rows(db_session)
    token = _tokens(client, '/experience/profile', 'profile')['csrf_token']
    assert client.get(path, follow_redirects=False).status_code == 302
    response = client.post(path, data={'csrf_token': token, 'name': '不得入库', 'symptoms': '不得入库',
                                      'medicine_name': '不得入库'}, follow_redirects=False)
    assert response.status_code == 302
    assert _business_rows(db_session) == before


def test_formal_runtime_keeps_guest_demo_open_and_real_private_routes_closed(experience_app):
    client = _guest(experience_app)
    experience_app.config.update(WECHAT_FORMAL_RUNTIME=True, WEB_PRIVATE_FEATURES_ENABLED=False)
    for path in _PAGES:
        assert client.get(path).status_code == 200
    _saved(_post(client, '/experience/profile', 'profile', username='正式态示例', age='69',
                 gender='未知', community='示例社区'), '/experience/profile')
    response = client.get('/health-diary', follow_redirects=False)
    assert response.status_code == 303
    assert urlsplit(response.headers['Location']).path == '/action'


def test_real_account_cannot_enter_or_mutate_guest_experience(experience_app, db_session):
    from core.db_models import User

    user = User(username='real-demo-boundary', role='user')
    user.set_password('integration-password')
    db_session.add(user)
    db_session.commit()
    client = _client(experience_app)
    with client.session_transaction() as session:
        session.update(_user_id=f'{user.id}:{user.auth_version}', _fresh=True, _csrf_token='real-user-csrf')
    for path in _PAGES:
        assert client.get(path).status_code == 403
    assert client.post('/experience/profile', data={'csrf_token': 'real-user-csrf', 'operation': 'profile',
                                                  'version': '1', 'age': '70'}).status_code == 403


def test_guest_store_failure_stops_loaded_identity_without_recursive_error_page(experience_app, monkeypatch):
    from services.guest_experience import GuestExperienceError

    client = _guest(experience_app)
    client.get('/experience/profile')
    store = experience_app.extensions['guest_experience_store']
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append(1)
        raise GuestExperienceError('临时体验服务不可用', 503)

    monkeypatch.setattr(store, 'get', unavailable)
    for path in ('/experience/profile', '/ml-prediction'):
        calls.clear()
        response = client.get(path)
        assert response.status_code == 503
        assert response.cache_control.no_store
        assert len(calls) <= 2
        assert '这次操作未保存' in response.get_data(as_text=True)


def test_guest_profile_age_and_diseases_reach_tools_and_prediction_apis(experience_app, monkeypatch):
    from core.time_utils import utcnow

    client = _guest(experience_app)
    _saved(_post(client, '/experience/profile', 'profile', username='年龄联调示例', age='77', gender='女',
                 community='都昌', has_chronic_disease='1', chronic_diseases=['高血压']), '/experience/profile')
    seen_ml, seen_chronic = [], []

    class Predictor:
        def predict_disease_risk(self, user_info, _weather):
            seen_ml.append(dict(user_info))
            return {'success': False, 'error': '合成预测仅验证输入'}

        def predict_individual_risk(self, user_info, _weather):
            seen_chronic.append(dict(user_info))
            return {'risk_level': '低', 'recommendations': []}

    weather = {'temperature': 28, 'temperature_max': 32, 'temperature_min': 23, 'humidity': 65,
               'pressure': 1005, 'wind_speed': 2.5, 'weather_condition': '多云', 'observed_at': utcnow().isoformat(),
               'quality_version': 1, 'data_source': 'QWeather', 'is_mock': False}
    predictor = Predictor()
    monkeypatch.setattr('blueprints.tools.get_weather_with_cache', lambda _location: (dict(weather), False))
    monkeypatch.setattr('services.api_service.get_weather_with_cache', lambda _location: (dict(weather), False))
    monkeypatch.setattr('blueprints.tools.get_ml_service', lambda: predictor)
    monkeypatch.setattr('services.ml_prediction_service.get_ml_service', lambda: predictor)
    monkeypatch.setattr('services.chronic_risk_service.get_chronic_service', lambda: predictor)
    response = client.get('/ml-prediction')
    assert response.status_code == 200
    assert 'RF 已退出生产预测' in response.get_data(as_text=True)
    csrf = _tokens(client, '/experience/profile', 'profile')['csrf_token']
    assert client.post('/ml-prediction', data={'csrf_token': csrf, 'location': '都昌', 'age': '18'}).status_code == 200
    assert client.post('/api/v1/ml/predict', json={'age': 18, 'gender': '男'},
                       headers={'X-CSRF-Token': csrf}).status_code == 410
    chronic = client.post('/api/v1/chronic/individual', json={'age': 18, 'gender': '男', 'chronic_diseases': ['糖尿病']},
                          headers={'X-CSRF-Token': csrf})
    assert chronic.status_code == 200 and chronic.json['success'] is True
    assert seen_ml == []
    assert seen_chronic == [{'age': 77, 'gender': '女', 'chronic_diseases': ['高血压']}]


@pytest.mark.parametrize('path', ['/health-assessment', '/ml-prediction'])
def test_expired_open_form_cannot_run_model_or_recreate_experience(experience_app, monkeypatch, path):
    from services.guest_experience import delete_experience, get_experience

    client = _guest(experience_app)
    opened = client.get(path)
    assert opened.status_code == 200
    csrf = next(item['value'] for item in _Page(opened.get_data(as_text=True)).inputs
                if item.get('name') == 'csrf_token')
    with client.session_transaction() as session:
        old_id = session['_user_id']
    # 模拟Redis到期删除，不修改全局时钟，避免影响CSRF和限流时钟。
    with experience_app.app_context():
        assert delete_experience(old_id) is True

    def must_not_run(*_args, **_kwargs):
        pytest.fail('过期游客的旧表单不得查询天气或执行模型')

    monkeypatch.setattr('services.user.profile_service.get_weather_with_cache', must_not_run)
    monkeypatch.setattr('blueprints.tools.get_weather_with_cache', must_not_run)
    monkeypatch.setattr('blueprints.tools.get_ml_service', must_not_run)
    monkeypatch.setattr('services.health_risk_service.HealthRiskService.assess_personal_weather_health_risk', must_not_run)
    data = {'csrf_token': csrf, 'location': '都昌', 'age': '65', 'outdoor_exposure': 'low',
            'symptom_level': 'none', 'hydration': 'good', 'medication_adherence': 'good', 'sleep_quality': 'good'}
    for response in (client.post(path, data=data), client.get(path), client.get('/experience/')):
        assert response.status_code == 409
        assert response.cache_control.no_store
        body = response.get_data(as_text=True)
        assert '游客体验已结束' in body
        assert any(urlsplit(link).path == '/guest' for link in _Page(body).links)
        with experience_app.app_context():
            assert get_experience(old_id, create=False) is None
        with client.session_transaction() as session:
            assert session['_user_id'] == old_id

    restarted = client.get('/guest', follow_redirects=False)
    assert restarted.status_code == 302
    with client.session_transaction() as session:
        assert session['_user_id'] != old_id
    assert _state(experience_app, client)['profile']['age'] == 65
    with experience_app.app_context():
        assert get_experience(old_id, create=False) is None
    assert client.get('/experience/').status_code == 200


def test_guest_entry_budget_is_shared_by_new_sessions_on_same_ip(experience_app):
    client = _client(experience_app)
    guest_ids = set()
    for _ in range(10):
        assert client.get('/guest', follow_redirects=False).status_code == 302
        with client.session_transaction() as session:
            guest_ids.add(session['_user_id'])
    assert len(guest_ids) == 10
    before = _state(experience_app, client)
    denied = client.get('/guest', follow_redirects=False)
    assert denied.status_code == 429
    assert denied.cache_control.no_store and int(denied.headers['Retry-After']) > 0
    assert _state(experience_app, client) == before
    empty_session = _client(experience_app)
    empty_session.environ_base['REMOTE_ADDR'] = client.environ_base['REMOTE_ADDR']
    assert empty_session.get('/guest', follow_redirects=False).status_code == 429
    # 单个地址达到额度不能误伤其他访客。
    assert _state(experience_app, _guest(experience_app))['profile']['age'] == 65


def test_new_guest_identity_does_not_reset_ip_mutation_budget(experience_app):
    client = _guest(experience_app)
    data = _tokens(client, '/experience/profile', 'profile')
    data.update(username='限流示例', age='70', gender='未知', community='示例社区')
    for _ in range(30):
        data['version'] = str(_state(experience_app, client)['version'])
        _saved(client.post('/experience/profile', data=data, follow_redirects=False), '/experience/profile')
    before = _state(experience_app, client)
    data['version'] = str(before['version'])
    denied = client.post('/experience/profile', data=data)
    assert denied.status_code == 429
    assert denied.cache_control.no_store and int(denied.headers['Retry-After']) > 0
    assert _state(experience_app, client) == before
    with client.session_transaction() as session:
        old_id = session['_user_id']
    assert client.get('/guest', follow_redirects=False).status_code == 302
    with client.session_transaction() as session:
        assert session['_user_id'] != old_id
    renewed = _state(experience_app, client)
    denied_again = _post(client, '/experience/profile', 'profile', username='新身份仍受限', age='80',
                         gender='未知', community='示例社区')
    assert denied_again.status_code == 429
    assert _state(experience_app, client) == renewed
    independent = _guest(experience_app)
    _saved(_post(independent, '/experience/profile', 'profile', username='独立访客', age='80',
                 gender='未知', community='示例社区'), '/experience/profile')


def test_guest_health_assessment_uses_profile_and_saves_only_temporary_state(
    experience_app, db_session, monkeypatch
):
    from core.time_utils import utcnow

    client = _guest(experience_app)
    _saved(_post(client, '/experience/profile', 'profile', username='评估资料示例', age='79',
                 gender='女', community='都昌', has_chronic_disease='1',
                 chronic_diseases=['高血压']), '/experience/profile')
    before = _state(experience_app, client)
    business_before = _business_rows(db_session)
    observed_at = utcnow().isoformat()
    weather = {
        'temperature': 31, 'temperature_max': 34, 'temperature_min': 26,
        'humidity': 68, 'pressure': 1006, 'weather_condition': '多云', 'wind_speed': 2.5,
        'pm25': 38, 'aqi': 62, 'observed_at': observed_at, 'air_observed_at': observed_at,
        'air_quality_available': True, 'quality_version': 1, 'data_source': 'QWeather',
        'is_mock': False, 'is_demo': False,
    }
    seen = []

    def assess(_self, profile, weather_input, screening=None):
        seen.append((dict(profile), dict(weather_input), dict(screening or {})))
        return {
            'risk_score': 57, 'risk_level': '中风险',
            'recommendations': ['合成评估建议仅用于测试'], 'disease_risks': {},
            'screening': screening, 'rule_version': 'guest-integration-only',
        }

    def must_not_notify(*_args, **_kwargs):
        pytest.fail('游客健康评估不得创建真实提醒')

    monkeypatch.setattr('services.user.profile_service.get_weather_with_cache',
                        lambda _location: (dict(weather), False))
    monkeypatch.setattr('services.health_risk_service.HealthRiskService.assess_personal_weather_health_risk', assess)
    monkeypatch.setattr('services.user.profile_service.create_notification', must_not_notify)
    csrf = _tokens(client, '/experience/profile', 'profile')['csrf_token']
    response = client.post('/health-assessment', data={
        'csrf_token': csrf, 'outdoor_exposure': 'low', 'symptom_level': 'none',
        'hydration': 'good', 'medication_adherence': 'good', 'sleep_quality': 'good',
        'age': '18', 'gender': '男',
    }, follow_redirects=False)
    assert response.status_code == 302
    assert urlsplit(response.headers['Location']).path == '/health-assessment'
    assert response.cache_control.no_store
    assert len(seen) == 1
    assert seen[0][0] == {
        'age': 79, 'gender': '女', 'community': '都昌',
        'has_chronic_disease': True, 'chronic_diseases': ['高血压'],
    }
    assert seen[0][1]['temperature'] == 31
    assert seen[0][2]['hydration'] == 'good'
    after = _state(experience_app, client)
    assert after['version'] == before['version'] + 1
    assert after['assessment']['risk_score'] == 57
    assert after['assessment']['risk_level'] == '中风险'
    assert json.loads(after['assessment']['recommendations']) == ['合成评估建议仅用于测试']
    assert _business_rows(db_session) == business_before
    cookie = client.get_cookie(experience_app.config['SESSION_COOKIE_NAME'])
    decoded = experience_app.session_interface.get_signing_serializer(experience_app).loads(cookie.value)
    assert 'guest_profile' not in decoded and 'guest_assessment' not in decoded
    serialized = json.dumps(decoded, ensure_ascii=False)
    for sensitive in ('评估资料示例', '合成评估建议仅用于测试', '高血压', 'guest-integration-only'):
        assert sensitive not in serialized
    assert '合成评估建议仅用于测试' in client.get('/health-assessment').get_data(as_text=True)
