"""资源消耗边界回归；全部外部调用使用假响应。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


class FakeGeocode:
    status_code = 200

    def __init__(self, data=None, raw=None):
        self.raw = raw if raw is not None else json.dumps(data or {
            'status': '1', 'geocodes': [{'location': '120.10,30.20'}],
        }).encode()

    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), chunk_size):
            yield self.raw[start:start + chunk_size]

    def close(self):
        pass


class FakeAI:
    status_code = 200
    headers = {}

    def json(self):
        return {'choices': [{'message': {'content': '测试建议'}}]}


def test_oversize_body_rejected_before_auth_or_parsing(app, client, db_session):
    from core.db_models import UsageEvent
    for route, content_type in [('/api/v1/events', 'application/json'),
                                ('/mp/api/v1/events', 'application/json'),
                                ('/login', 'application/x-www-form-urlencoded')]:
        response = client.post(route, data=b'x' * (1024 * 1024 + 1), content_type=content_type)
        assert response.status_code == 413
        assert response.get_json()['error'] == 'request_too_large'
    assert UsageEvent.query.count() == 0


def test_chunked_body_and_deep_json_are_bounded(app, authenticated_client, db_session):
    import io
    from core.db_models import UsageEvent
    with authenticated_client.session_transaction() as session:
        csrf = session['_csrf_token']
    response = authenticated_client.open('/api/v1/events', method='POST', headers={'X-CSRF-Token': csrf},
        environ_overrides={'wsgi.input': io.BytesIO(b'x' * (1024 * 1024 + 1)),
                           'wsgi.input_terminated': True, 'CONTENT_LENGTH': '', 'CONTENT_TYPE': 'application/json'})
    assert response.status_code == 413
    response = authenticated_client.post('/api/v1/events', data='[' * 2000 + '0' + ']' * 2000,
                                         content_type='application/json', headers={'X-CSRF-Token': csrf})
    assert response.status_code == 400
    assert UsageEvent.query.count() == 0


def test_web_pair_limit_and_pagination(app, authenticated_client, db_session, monkeypatch):
    from core.db_models import Pair, User
    from services.user._common import _create_pair_record
    user_id = User.query.filter_by(username='testuser').one().id
    for number in range(3):
        _create_pair_record(user_id, f'测试地点{number}')
    db_session.commit()
    app.config.update(PAIR_LIST_PAGE_SIZE=2, PAIR_MAX_PER_USER=3)
    calls = []
    monkeypatch.setattr('services.user.caregiver_service.resolve_location',
                        lambda label: calls.append(label) or {'location_code': '', 'display_name': label})
    first = authenticated_client.get('/pairs')
    assert first.status_code == 200 and len(calls) == 2
    assert '下一页' in first.get_data(as_text=True)
    calls.clear()
    second = authenticated_client.get('/pairs?page=2')
    assert second.status_code == 200 and len(calls) == 1
    with authenticated_client.session_transaction() as session:
        csrf = session['_csrf_token']
    response = authenticated_client.post('/pairs', data={'csrf_token': csrf, 'location_query': '另一地点'})
    assert response.status_code == 302
    assert Pair.query.filter_by(caregiver_id=user_id).count() == 3


def test_ai_route_aliases_share_rate_limit(app, authenticated_client, db_session, monkeypatch):
    from core.extensions import limiter
    limiter.reset()
    app.config.update(RATE_LIMIT_AI='1 per hour', SILICONFLOW_API_KEY='fake-key', FEATURE_WEB_AI=True)
    calls = []
    monkeypatch.setattr('services.ai_question_service.requests.post', lambda *a, **kw: calls.append(kw) or FakeAI())
    with authenticated_client.session_transaction() as session:
        csrf = session['_csrf_token']
    payload = {'question': '天气如何', 'model': app.config['AI_ALLOWED_MODELS'][0]}
    headers = {'X-CSRF-Token': csrf}
    assert authenticated_client.post('/api/v1/ai/ask', json=payload, headers=headers).status_code == 200
    assert authenticated_client.post('/api/ai/ask', json=payload, headers=headers).status_code == 429
    assert len(calls) == 1
    limiter.reset()


@pytest.mark.parametrize('precreated', [False, True])
def test_budget_migration_matches_models(tmp_path, precreated):
    from pathlib import Path
    import importlib.util
    from sqlalchemy import create_engine, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from core.resource_budget import ResourceBudget, ResourceLease
    path = Path(__file__).resolve().parents[1] / 'migrations/versions/0034_security_budgets.py'
    spec = importlib.util.spec_from_file_location('budget_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f'sqlite:///{tmp_path / "budget-migration.db"}')
    with engine.begin() as connection:
        if precreated:
            ResourceBudget.__table__.create(connection)
            ResourceLease.__table__.create(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        for model in (ResourceBudget, ResourceLease):
            assert set(column['name'] for column in inspector.get_columns(model.__tablename__)) == set(model.__table__.c.keys())
        connection.exec_driver_sql("INSERT INTO resource_budgets VALUES ('spent', 1, CURRENT_TIMESTAMP)")
        with pytest.raises(RuntimeError, match='降级会重置额度'):
            migration.downgrade()
        assert connection.exec_driver_sql('SELECT used FROM resource_budgets').scalar_one() == 1
        connection.exec_driver_sql('DELETE FROM resource_budgets')
        migration.downgrade()
        assert inspect(connection).get_table_names() == []
        from core.db_models import LocationCache
        LocationCache.__table__.create(connection)
        connection.execute(LocationCache.__table__.insert().values({
            'query': '业务地点', 'location_code': '120.1,30.2', 'raw_json': '{"private_address":"duplicate"}',
        }))
        migration.upgrade()
        row = connection.execute(LocationCache.__table__.select()).mappings().one()
        assert row['raw_json'] is None
        assert row['query'] == '业务地点' and row['location_code'] == '120.1,30.2'
        migration.downgrade()
        connection.exec_driver_sql('CREATE TABLE resource_budgets (key VARCHAR(180) PRIMARY KEY, used BIGINT)')
        with pytest.raises(RuntimeError, match='结构不兼容'):
            migration.upgrade()
    engine.dispose()


@pytest.mark.parametrize('meta', [
    {'value': '汉' * 800},
    {'a': {'b': {'c': {'d': {'e': {'f': 1}}}}}},
    {str(i): i for i in range(65)},
    [1] * 65,
])
def test_event_metadata_rejected_without_database_write(app, authenticated_client, db_session, meta):
    from core.db_models import UsageEvent, User
    from core.usage import create_api_token
    token = create_api_token(User.query.filter_by(username='testuser').one().id)
    baseline = UsageEvent.query.count()
    with authenticated_client.session_transaction() as session:
        csrf = session['_csrf_token']
    for route, headers in [('/api/v1/events', {'X-CSRF-Token': csrf}),
                           ('/mp/api/v1/events', {'Authorization': f'Bearer {token}'})]:
        response = authenticated_client.post(route, json={'event_type': 'template_copy', 'meta': meta}, headers=headers)
        assert response.status_code == 400
        assert UsageEvent.query.count() == baseline
        good = authenticated_client.post(route, json={'event_type': 'template_copy', 'meta': {'via': 'mp_api'}}, headers=headers)
        assert good.status_code == 200
        baseline += 1
        assert UsageEvent.query.count() == baseline


def test_pair_limit_serializes_concurrent_creates(app, db_session):
    from core.db_models import Pair, User
    from core.extensions import db
    from core.resource_budget import ResourceLimitError
    from services.user._common import _create_pair_record
    user = User(username='parallel-pair', role='user')
    user.set_password('test-only-password')
    db_session.add(user)
    db_session.commit()
    user_id = user.id
    app.config['PAIR_MAX_PER_USER'] = 2

    def create(_):
        with app.app_context():
            try:
                _create_pair_record(user_id, '九江')
                db.session.commit()
                return True
            except ResourceLimitError:
                db.session.rollback()
                return False
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(create, range(10))) == 2
    assert Pair.query.filter_by(caregiver_id=user_id).count() == 2


def test_mp_pair_limit_rolls_back_new_member(app, client, db_session):
    from core.db_models import FamilyMember, User
    from core.usage import create_api_token
    user = User(username='pair-mp', role='user')
    user.set_password('test-only-password')
    db_session.add(user)
    db_session.commit()
    token = create_api_token(user.id)
    app.config['PAIR_MAX_PER_USER'] = 1
    payload = {'name': '测试家人', 'age': 70, 'location_query': '九江'}
    headers = {'Authorization': f'Bearer {token}'}
    assert client.post('/mp/api/v1/health-consent', headers=headers, json={
        'consent': True, 'health_consent_version': app.config['WX_MINIPROGRAM_PRIVACY_VERSION'],
    }).status_code == 200
    assert client.post('/mp/api/v1/elders', json=payload, headers=headers).status_code == 200
    result = client.post('/mp/api/v1/elders', json=payload, headers=headers)
    assert result.status_code == 429
    assert result.get_json()['error'] == 'pair_limit_reached'
    assert FamilyMember.query.filter_by(user_id=user.id).count() == 1


def test_legacy_link_redemption_obeys_cap_and_preserves_link(app, db_session):
    from core.db_models import Pair, PairLink, User
    from services.user._common import _create_pair_record, _create_pair_link_record
    from services.public_service import _resolve_pair
    user = User(username='legacy-link-cap', role='user')
    user.set_password('test-only-password')
    db_session.add(user)
    db_session.flush()
    _create_pair_record(user.id, '九江')
    link, token = _create_pair_link_record(user.id, '九江', flush=True)
    db_session.commit()
    link_id, short_code, user_id = link.id, link.short_code, user.id
    app.config['PAIR_MAX_PER_USER'] = 1
    with app.test_request_context('/action'):
        pair, error = _resolve_pair(short_code, token)
        assert pair is None and '上限' in error
    db_session.expire_all()
    assert Pair.query.filter_by(caregiver_id=user_id).count() == 1
    link = db_session.get(PairLink, link_id)
    assert link.status == 'active' and link.redeemed_at is None
    app.config['PAIR_MAX_PER_USER'] = 2
    with app.test_request_context('/action'):
        pair, error = _resolve_pair(short_code, token)
        assert error is None and pair.short_code == short_code
    db_session.expire_all()
    assert Pair.query.filter_by(caregiver_id=user_id).count() == 2
    assert db_session.get(PairLink, link_id).status == 'redeemed'


def test_ai_shared_and_per_user_budget_at_provider_boundary(app, db_session, monkeypatch):
    from core.resource_budget import ResourceLimitError
    from services.ai_question_service import AIQuestionService
    calls = []
    monkeypatch.setattr('services.ai_question_service.requests.post', lambda *a, **kw: calls.append(kw) or FakeAI())
    app.config.update(AI_USER_DAILY_LIMIT=1, AI_DAILY_LIMIT=2)
    service = AIQuestionService('fake', 'https://api.siliconflow.cn/v1', ['model'])
    assert service.ask('天气如何', 'model', user_id=1) == '测试建议'
    with pytest.raises(ResourceLimitError):
        service.ask('再问一次', 'model', user_id=1)
    assert service.ask('天气如何', 'model', user_id=2) == '测试建议'
    with pytest.raises(ResourceLimitError):
        service.ask('天气如何', 'model', user_id=3)
    assert len(calls) == 2


def test_ai_retry_reserves_again_and_backend_failure_denies(app, db_session, monkeypatch):
    from core.resource_budget import ResourceLimitError, ResourceBudgetUnavailable, ResourceBudget
    from core.extensions import db
    from services.ai_question_service import AIQuestionService
    response = FakeAI()
    response.status_code = 503
    calls = []
    monkeypatch.setattr('services.ai_question_service.requests.post', lambda *a, **kw: calls.append(kw) or response)
    monkeypatch.setattr('services.ai_question_service.time.sleep', lambda _: None)
    app.config['AI_DAILY_LIMIT'] = 1
    service = AIQuestionService('fake', 'https://api.siliconflow.cn/v1', ['model'])
    with pytest.raises(ResourceLimitError):
        service.ask('天气如何', 'model', user_id=1)
    assert len(calls) == 1
    ResourceBudget.__table__.drop(db.engine)
    with pytest.raises(ResourceBudgetUnavailable):
        service.ask('天气如何', 'model', user_id=2)
    assert len(calls) == 1


def test_atomic_budget_concurrency_and_token_cap(app, db_session):
    from core.resource_budget import reserve, ResourceLimitError, ResourceBudget
    app.config.update(AI_DAILY_LIMIT=7, AI_DAILY_TOKEN_LIMIT=70)

    def consume(i):
        with app.app_context():
            try:
                reserve('AI', units=10, user_id=i)
                return True
            except ResourceLimitError:
                return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(20))) == 7
    total = ResourceBudget.query.filter(ResourceBudget.key.like('AI:tokens:day:%')).one()
    assert total.used == 70


def test_geocode_negative_cache_and_response_bound(app, db_session, monkeypatch):
    from services.location_resolver import resolve_location
    from core.db_models import LocationCache
    app.config.update(AMAP_WEB_SERVICE_KEY='fake-service', CITY_LOCATION_MAP={})
    calls = []
    monkeypatch.setattr('services.location_resolver.requests.get', lambda *a, **kw: calls.append(kw) or FakeGeocode(raw=b'x' * 65537))
    assert resolve_location('不存在的地点')['provider'] == 'fallback'
    assert resolve_location('不存在的地点')['provider'] == 'fallback'
    assert len(calls) == 1
    row = LocationCache.query.one()
    assert row.provider == 'negative' and row.raw_json is None


def test_geocode_misses_coalesce_and_cache_evicts(app, db_session, monkeypatch):
    from services.location_resolver import resolve_location
    from core.db_models import LocationCache
    app.config.update(AMAP_WEB_SERVICE_KEY='fake-service', CITY_LOCATION_MAP={}, LOCATION_CACHE_MAX_ROWS=2)
    calls = []

    def request(*a, **kw):
        calls.append(kw)
        time.sleep(0.15)
        return FakeGeocode()

    monkeypatch.setattr('services.location_resolver.requests.get', request)

    def resolve(_):
        with app.app_context():
            return resolve_location('同一个地点')

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert all(row['provider'] == 'amap' for row in pool.map(resolve, range(6)))
    assert len(calls) == 1
    for i in range(5):
        resolve_location(f'120.{i},30.20')
    assert LocationCache.query.count() == 2
    assert LocationCache.query.filter_by(query_text='同一个地点').first() is None


def test_geocode_daily_quota_and_backend_fail_closed(app, db_session, monkeypatch):
    from services.location_resolver import resolve_location
    from core.resource_budget import ResourceBudget
    from core.extensions import db
    app.config.update(AMAP_WEB_SERVICE_KEY='fake-service', GEOCODE_USER_DAILY_LIMIT=1)
    calls = []
    monkeypatch.setattr('services.location_resolver.requests.get', lambda *a, **kw: calls.append(kw) or FakeGeocode())
    assert resolve_location('地点甲', user_id=1)['provider'] == 'amap'
    assert resolve_location('地点乙', user_id=1)['provider'] == 'fallback'
    assert len(calls) == 1
    ResourceBudget.__table__.drop(db.engine)
    assert resolve_location('地点丙', user_id=2)['provider'] == 'fallback'
    assert len(calls) == 1
