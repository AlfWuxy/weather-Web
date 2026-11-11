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
    app.config.update(RATE_LIMIT_AI='1 per hour', SILICONFLOW_API_KEY='fake-key')
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
    path = Path(__file__).resolve().parents[1] / 'migrations/versions/0011_security_budgets.py'
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
        response = authenticated_client.post(route, json={'event_type': 'template_view', 'meta': meta}, headers=headers)
        assert response.status_code == 400
        assert UsageEvent.query.count() == baseline
        good = authenticated_client.post(route, json={'event_type': 'template_view', 'meta': {'via': 'button'}}, headers=headers)
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
    payload = {'name': '测试家人', 'location_query': '九江'}
    headers = {'Authorization': f'Bearer {token}'}
    assert client.post('/mp/api/v1/elders', json=payload, headers=headers).status_code == 200
    result = client.post('/mp/api/v1/elders', json=payload, headers=headers)
    assert result.status_code == 429
    assert result.get_json()['error'] == 'pair_limit_reached'
    assert FamilyMember.query.filter_by(user_id=user.id).count() == 1


def test_legacy_link_redemption_obeys_cap_and_preserves_link(app, db_session):
    from core.db_models import Pair, PairLink, User
    from core.security import hash_short_code
    from services.user._common import _create_pair_record, _create_pair_link_record
    from services.public_service import _resolve_pair
    user = User(username='legacy-link-cap', role='user')
    user.set_password('test-only-password')
    db_session.add(user)
    db_session.flush()
    _create_pair_record(user.id, '九江')
    link, token = _create_pair_link_record(user.id, '九江', flush=True)
    short_code = link.issued_short_code
    assert short_code
    db_session.commit()
    link_id, user_id = link.id, user.id
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
        assert error is None
        assert pair.short_code is None
        assert pair.short_code_hash == hash_short_code(short_code)
    db_session.expire_all()
    assert Pair.query.filter_by(caregiver_id=user_id).count() == 2
    assert db_session.get(PairLink, link_id).status == 'redeemed'


def test_ai_shared_and_per_user_budget_at_provider_boundary(app, db_session, monkeypatch):
    from core.resource_budget import ResourceLimitError
    from services.ai_question_service import AIQuestionService
    calls = []
    monkeypatch.setattr('services.ai_question_service.requests.post', lambda *a, **kw: calls.append(kw) or FakeAI())
    app.config.update(AI_USER_DAILY_LIMIT=1, AI_DAILY_LIMIT=2)
    service = AIQuestionService('fake', 'https://example.invalid', ['model'])
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
    service = AIQuestionService('fake', 'https://example.invalid', ['model'])
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


def test_browser_key_never_falls_back_to_server_or_legacy(app, db_session, monkeypatch):
    from core.config import configure_app
    import logging
    monkeypatch.setenv('AMAP_KEY', 'legacy-secret-xxxxxxxxxxxxxxxx')
    monkeypatch.setenv('AMAP_WEB_SERVICE_KEY', 'server-secret-xxxxxxxxxxxxxxxx')
    monkeypatch.setenv('AMAP_JS_API_KEY', '')
    configure_app(app, logging.getLogger('test'))
    with app.test_request_context('/community-risk'):
        context = {}
        app.update_template_context(context)
        assert not context.get('amap_key')
    app.config['AMAP_JS_API_KEY'] = 'browser-public-xxxxxxxxxxxxxxxx'
    with app.test_request_context('/community-risk'):
        context = {}
        app.update_template_context(context)
        assert context['amap_key'] == 'browser-public-xxxxxxxxxxxxxxxx'
        assert 'server-secret' not in json.dumps(context, default=str)
        assert 'legacy-secret' not in json.dumps(context, default=str)
    monkeypatch.setenv('AMAP_JS_API_KEY', 'server-secret-xxxxxxxxxxxxxxxx')
    with pytest.raises(RuntimeError, match='不同用途'):
        configure_app(app, logging.getLogger('test'))


def _forecast_daily(days):
    from datetime import timedelta
    from core.time_utils import today_local

    start = today_local()
    return {
        'success': True,
        'daily': [
            {
                'date': (start + timedelta(days=index)).strftime('%Y-%m-%d'),
                'temperature_max': 31,
                'temperature_min': 24,
                'temperature_mean': 27,
                'humidity': 60,
                'data_source': 'QWeather',
                'is_mock': False,
            }
            for index in range(days)
        ],
        'meta': {'source': 'QWeather'},
    }


class _BudgetForecastFetcher:
    def __init__(self):
        self.qweather_calls = 0
        self.forecast_calls = 0

    def get_qweather_daily_forecast(self, _location, days=7):
        self.qweather_calls += 1
        return _forecast_daily(days)

    def get_weather_forecast(self, _location, days=7):
        self.forecast_calls += 1
        return [{'date': '2026-07-01', 'temperature_max': 30, 'temperature_min': 20, 'is_mock': True}]


def _weather_budget_used():
    from core.resource_budget import ResourceBudget

    rows = ResourceBudget.query.filter(ResourceBudget.key.like('WEATHER:%')).all()
    assert rows
    assert {row.used for row in rows} == {rows[0].used}
    return rows[0].used


def test_forecast_upstream_budget_skips_cache_and_keeps_web_requests(app, db_session):
    from core.db_models import ForecastCache
    from core.resource_budget import ResourceBudget
    from core.weather import (
        get_forecast_with_cache,
        get_qweather_forecast_with_cache,
        register_weather_fetcher,
    )

    fetcher = _BudgetForecastFetcher()
    with app.app_context():
        app.config.update(
            DEMO_MODE=False,
            WEATHER_UPSTREAM_HOURLY_LIMIT=1,
            WEATHER_UPSTREAM_DAILY_LIMIT=1,
            WEATHER_UPSTREAM_MONTHLY_LIMIT=1,
            WEATHER_SYNC_BYPASS=False,
        )
        app.extensions['redis_client'] = None
        register_weather_fetcher(fetcher)

        fresh, from_cache, meta = get_qweather_forecast_with_cache('都昌', days=7)
        assert from_cache is False and len(fresh) == 7 and meta['source'] == 'QWeather'
        assert fetcher.qweather_calls == 1
        assert _weather_budget_used() == 1

        cached, from_cache, _meta = get_qweather_forecast_with_cache('都昌', days=7)
        assert from_cache is True and cached == fresh
        assert fetcher.qweather_calls == 1
        assert _weather_budget_used() == 1
        assert ResourceBudget.query.filter(ResourceBudget.key.like('MAP:%')).count() == 0

        app.config['WEATHER_SYNC_BYPASS'] = True
        synced, sync_cached, sync_meta = get_qweather_forecast_with_cache('都昌', days=6)
        assert sync_cached is False and len(synced) == 6 and sync_meta['source'] == 'QWeather'
        assert fetcher.qweather_calls == 2
        assert _weather_budget_used() == 1

        with app.test_request_context('/dashboard'):
            denied, denied_cached, denied_meta = get_qweather_forecast_with_cache('都昌', days=5)
        assert denied == [] and denied_cached is False
        assert denied_meta['error'] == 'upstream_budget'
        assert fetcher.qweather_calls == 2
        assert _weather_budget_used() == 1
        assert ForecastCache.query.filter_by(days=5).count() == 0

        app.config['WEATHER_SYNC_BYPASS'] = False
        denied_forecast, forecast_cached = get_forecast_with_cache('都昌', days=4)
        assert denied_forecast == [] and forecast_cached is False
        assert fetcher.forecast_calls == 0
        assert ForecastCache.query.filter_by(days=4).count() == 0


def test_forecast_budget_denial_is_not_cached(app, db_session):
    from core.db_models import ForecastCache
    from core.weather import get_forecast_with_cache, get_qweather_forecast_with_cache, register_weather_fetcher

    fetcher = _BudgetForecastFetcher()
    with app.app_context():
        app.config.update(
            DEMO_MODE=False,
            WEATHER_UPSTREAM_HOURLY_LIMIT=0,
            WEATHER_UPSTREAM_DAILY_LIMIT=0,
            WEATHER_UPSTREAM_MONTHLY_LIMIT=0,
            WEATHER_SYNC_BYPASS=False,
        )
        app.extensions['redis_client'] = None
        register_weather_fetcher(fetcher)

        with app.test_request_context('/api/forecast'):
            denied, denied_cached, denied_meta = get_qweather_forecast_with_cache('都昌', days=7)
        assert denied == [] and denied_cached is False
        assert denied_meta['error'] == 'upstream_budget'
        empty, from_cache = get_forecast_with_cache('都昌', days=3)
        assert empty == [] and from_cache is False
        assert fetcher.qweather_calls == fetcher.forecast_calls == 0
        assert ForecastCache.query.count() == 0

        app.config.update(
            WEATHER_UPSTREAM_HOURLY_LIMIT=5,
            WEATHER_UPSTREAM_DAILY_LIMIT=5,
            WEATHER_UPSTREAM_MONTHLY_LIMIT=5,
        )
        restored, from_cache, _meta = get_qweather_forecast_with_cache('都昌', days=7)
        assert from_cache is False and len(restored) == 7
        generic, generic_cached = get_forecast_with_cache('都昌', days=3)
        assert generic_cached is False and generic
        again, again_cached = get_forecast_with_cache('都昌', days=3)
        assert again_cached is True and again == generic
        assert fetcher.qweather_calls == 1
        assert fetcher.forecast_calls == 1


def test_workbench_openmeteo_reserves_weather_budget_on_miss_only(app, db_session, monkeypatch):
    from core.resource_budget import ResourceBudget
    from services import heat_risk_workbench_forecast as forecast_service

    calls = []

    def fake_get(*_args, **_kwargs):
        from datetime import timedelta
        from core.time_utils import today_local

        calls.append(1)
        start = today_local()

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    'daily': {
                        'time': [(start + timedelta(days=index)).isoformat() for index in range(7)],
                        'temperature_2m_max': [36] * 7,
                        'temperature_2m_min': [24] * 7,
                    }
                }

        return Response()

    monkeypatch.setattr(forecast_service.requests, 'get', fake_get)
    with app.app_context():
        app.config.update(
            WEATHER_UPSTREAM_HOURLY_LIMIT=1,
            WEATHER_UPSTREAM_DAILY_LIMIT=1,
            WEATHER_UPSTREAM_MONTHLY_LIMIT=1,
            WEATHER_SYNC_BYPASS=False,
        )
        app.extensions['redis_client'] = None
        first = forecast_service.get_openmeteo_forecast_with_cache('都昌县')
        second = forecast_service.get_openmeteo_forecast_with_cache('都昌县')
        assert len(first) == 7 and second == first
        assert calls == [1]
        assert _weather_budget_used() == 1
        assert ResourceBudget.query.filter(ResourceBudget.key.like('MAP:%')).count() == 0

        denied = forecast_service.get_openmeteo_forecast_with_cache('九江')
        assert denied == []
        assert calls == [1]


def test_scheduled_sync_bypass_does_not_stick_to_web_config(app, db_session, monkeypatch):
    from services.pipelines import sync_weather_cache as cache_pipeline
    from services.pipelines import sync_weather_data as daily_pipeline

    monkeypatch.setattr(daily_pipeline, 'app', app)
    monkeypatch.setattr(cache_pipeline, 'app', app)
    seen = []

    class _Daily:
        def get_current_weather(self, _location):
            seen.append(('daily', bool(app.config.get('WEATHER_SYNC_BYPASS'))))
            return None

    class _Cache:
        def get_current_weather(self, _location):
            seen.append(('cache', bool(app.config.get('WEATHER_SYNC_BYPASS'))))
            return None

    monkeypatch.setattr(daily_pipeline, 'WeatherService', lambda: _Daily())
    monkeypatch.setattr(cache_pipeline, 'WeatherService', lambda: _Cache())
    app.config['WEATHER_SYNC_BYPASS'] = False

    daily_pipeline.sync_daily_weather(location='都昌县')
    cache_pipeline.sync_weather_cache(locations=['都昌县'], update_daily=False)

    assert seen == [('daily', True), ('cache', True)]
    assert app.config.get('WEATHER_SYNC_BYPASS') is False
