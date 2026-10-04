# -*- coding: utf-8 -*-
"""游客临时存储的隔离、过期、容量和原子写入契约。"""

import ast
from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import shutil
import subprocess
import threading
import time

from flask import Flask
import pytest

from services import guest_experience as experience
from services.guest_experience import GuestExperienceError, get_experience, mutate_experience

GUEST_A = 'guest:aaaaaaaaaaaaaaaa'
GUEST_B = 'guest:bbbbbbbbbbbbbbbb'


@pytest.fixture
def guest_app():
    app = Flask(__name__)
    app.config.update(TESTING=True, DEBUG=False, REDIS_URL='', WEATHER_CACHE_REDIS_URL='')
    return app


def change(state, operation, payload, guest_id=GUEST_A):
    return mutate_experience(guest_id, operation, payload, state['version'])


def assert_error(status, function, *args):
    with pytest.raises(GuestExperienceError) as caught:
        function(*args)
    assert caught.value.status_code == status
    assert caught.value.message
    return caught.value


def diary(member_id, **kwargs):
    return {'member_id': member_id, 'date': '2026-10-04', 'symptoms': '示例不适',
            'severity': '轻微', 'notes': '', **kwargs}


def medication(member_id, **kwargs):
    return {'member_id': member_id, 'name': '示例用药', 'time': '08:30', 'note': '', **kwargs}


def test_seed_is_isolated_copy_with_opaque_demo_ids(guest_app):
    with guest_app.app_context():
        assert get_experience(GUEST_A, create=False) is None
        state = get_experience(GUEST_A)
        other = get_experience(GUEST_B)
        assert state['schema_version'] == 1
        assert 0 < state['version'] < 2 ** 53
        assert 7199 <= state['expires_at'] - time.time() <= 7200
        assert state['profile'] == {
            'username': '示例长者', 'age': 65, 'gender': '未知',
            'community': experience.DEFAULT_CITY_LABEL,
            'has_chronic_disease': False, 'chronic_diseases': [],
        }
        assert state['members'][0]['name'] == '示例家人'
        assert state['members'][0]['id'].startswith('demo_member_')
        assert state['members'][0]['id'] != other['members'][0]['id']
        assert state['diaries'] == state['medications'] == []
        assert state['checkins'] == {}
        state['profile']['username'] = '只改副本'
        assert get_experience(GUEST_A)['profile']['username'] == '示例长者'
        assert GUEST_A not in next(iter(guest_app.extensions['guest_experience_store']._states))


@pytest.mark.parametrize('guest_id', [None, 1, '1', 'guest:', 'guest:short',
                                     'guest:aaaaaaaaaaaa\n', 'guest:../../account',
                                     'user:aaaaaaaaaaaaaaaa', 'guest:' + 'a' * 129])
def test_reject_non_guest_and_malformed_identifiers(guest_app, guest_id):
    with guest_app.app_context():
        assert_error(400, get_experience, guest_id)
        assert 'guest_experience_store' not in guest_app.extensions


def test_profile_is_demo_only_and_normalizes_chronic_diseases(guest_app):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        state = change(state, 'profile', {'username': '体验姓名', 'age': 80, 'gender': '女',
                       'has_chronic_disease': True, 'chronic_diseases': ['高血压']})
        assert state['profile']['chronic_diseases'] == ['高血压']
        state = change(state, 'profile', {'has_chronic_disease': False})
        assert state['profile']['chronic_diseases'] == []
        assert get_experience(GUEST_B)['profile']['username'] == '示例长者'


@pytest.mark.parametrize('payload', [
    {'age': True}, {'age': '65'}, {'age': 17}, {'age': 111}, {'gender': '无效'},
    {'has_chronic_disease': 'false'}, {'chronic_diseases': ['非列表疾病']},
    {'chronic_diseases': [['高血压']]}, {'chronic_diseases': ['高血压', '高血压']},
    {'chronic_diseases': '高血压'}, {'username': 'a' * 31}, {'username': ''},
    {'role': 'admin'}, {'community': 'a' * 61}, {'username': '\x00'}, {'username': '\ud800'},
])
def test_invalid_profile_does_not_modify_saved_state(guest_app, payload):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        assert_error(400, change, state, 'profile', payload)
        assert get_experience(GUEST_A) == state


def test_full_member_checkin_diary_medication_flow_and_cascade(guest_app):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        member_id = state['members'][0]['id']
        state = change(state, 'member_save', {'id': member_id, 'name': '修改示例家人',
                                              'age': 73, 'relationship': '长辈'})
        assert state['members'][0]['age'] == 73
        state = change(state, 'checkin', {'member_id': member_id, 'actions': ['drink_water', 'rest']})
        assert state['checkins'][member_id]['reviewed'] is False
        state = change(state, 'review', {'member_id': member_id})
        assert state['checkins'][member_id]['reviewed'] is True
        state = change(state, 'diary_save', diary(member_id))
        diary_id = state['diaries'][0]['id']
        state = change(state, 'diary_save', diary(member_id, id=diary_id, severity='明显'))
        assert len(state['diaries']) == 1
        assert state['diaries'][0]['severity'] == '明显'
        state = change(state, 'medication_save', medication(member_id))
        medication_id = state['medications'][0]['id']
        state = change(state, 'medication_save', medication(member_id, id=medication_id, time='21:10'))
        assert len(state['medications']) == 1
        assert state['medications'][0]['time'] == '21:10'
        state = change(state, 'member_delete', {'id': member_id})
        assert state['members'] == state['diaries'] == state['medications'] == []
        assert state['checkins'] == {}


def test_deleting_individual_records_preserves_member(guest_app):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        member_id = state['members'][0]['id']
        state = change(state, 'diary_save', diary(member_id))
        state = change(state, 'medication_save', medication(member_id))
        state = change(state, 'diary_delete', {'id': state['diaries'][0]['id']})
        state = change(state, 'medication_delete', {'id': state['medications'][0]['id']})
        assert len(state['members']) == 1
        assert state['diaries'] == state['medications'] == []


def test_foreign_member_and_record_ids_are_not_accessible(guest_app):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        other = get_experience(GUEST_B)
        other_id = other['members'][0]['id']
        other = change(other, 'diary_save', diary(other_id), GUEST_B)
        other = change(other, 'medication_save', medication(other_id), GUEST_B)
        attempts = [
            ('member_delete', {'id': other_id}),
            ('member_save', {'id': other_id, 'name': '示例', 'age': 70, 'relationship': '家人'}),
            ('checkin', {'member_id': other_id, 'actions': ['rest']}),
            ('review', {'member_id': other_id}), ('diary_save', diary(other_id)),
            ('medication_save', medication(other_id)),
            ('diary_delete', {'id': other['diaries'][0]['id']}),
            ('medication_delete', {'id': other['medications'][0]['id']}),
            ('diary_save', diary(state['members'][0]['id'], id=other['diaries'][0]['id'])),
            ('medication_save', medication(state['members'][0]['id'], id=other['medications'][0]['id'])),
        ]
        for operation, payload in attempts:
            assert_error(404, change, state, operation, payload)
        assert get_experience(GUEST_A) == state
        assert get_experience(GUEST_B) == other


@pytest.mark.parametrize('operation,payload', [
    ('member_save', {'name': 'a' * 31, 'age': 65, 'relationship': '家人'}),
    ('member_save', {'name': '示例', 'age': 65, 'relationship': 'a' * 21}),
    ('checkin', {'actions': []}), ('checkin', {'actions': ['rest', 'rest']}),
    ('checkin', {'actions': ['send_notification']}), ('checkin', {'actions': [1]}),
    ('diary_save', {'date': '2026-02-30', 'symptoms': '示例', 'severity': '轻微'}),
    ('diary_save', {'date': '20261004', 'symptoms': '示例', 'severity': '轻微'}),
    ('diary_save', {'date': '2026-10-04', 'symptoms': '示例', 'severity': '严重'}),
    ('medication_save', {'name': '示例', 'time': '24:00'}),
    ('medication_save', {'name': '示例', 'time': '8:30'}),
    ('medication_save', {'name': 'a' * 81, 'time': '08:30'}),
    ('reset', {'expires_at': 9999999999}), ('unknown', {}),
])
def test_malformed_operations_leave_state_unchanged(guest_app, operation, payload):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        if operation in ('checkin', 'diary_save', 'medication_save'):
            payload = {**payload, 'member_id': state['members'][0]['id']}
        assert_error(400, change, state, operation, payload)
        assert get_experience(GUEST_A) == state


def test_missing_checkin_cannot_be_reviewed(guest_app):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        assert_error(404, change, state, 'review', {'member_id': state['members'][0]['id']})


@pytest.mark.parametrize('version', [None, True, '1', 0, -1, 2 ** 53])
def test_invalid_versions_are_rejected(guest_app, version):
    with guest_app.app_context():
        get_experience(GUEST_A)
        assert_error(400, mutate_experience, GUEST_A, 'reset', {}, version)


def test_state_expiry_is_fixed_for_read_edit_and_reset(guest_app, monkeypatch):
    now = [1_800_000_000]
    monkeypatch.setattr(experience.time, 'time', lambda: now[0])
    with guest_app.app_context():
        initial = get_experience(GUEST_A)
        now[0] += 600
        edited = change(initial, 'profile', {'age': 75})
        now[0] += 600
        reset = change(edited, 'reset', {})
        assert reset['profile']['age'] == 65
        assert reset['members'][0]['id'] != initial['members'][0]['id']
        assert reset['version'] == initial['version'] + 2
        assert reset['expires_at'] == edited['expires_at'] == initial['expires_at']
        assert_error(409, change, initial, 'profile', {'age': 60})
        now[0] = initial['expires_at']
        assert get_experience(GUEST_A, create=False) is None
        assert_error(409, change, reset, 'reset', {})
        renewed = get_experience(GUEST_A)
        assert renewed['version'] != initial['version']
        assert_error(409, change, initial, 'reset', {})


def test_max_members_diaries_and_medications(guest_app):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        for _ in range(2):
            state = change(state, 'member_save', {'name': '新增示例', 'age': 60, 'relationship': '家人'})
        assert_error(429, change, state, 'member_save', {'name': '超量示例', 'age': 60, 'relationship': '家人'})
        member_id = state['members'][0]['id']
        for _ in range(20):
            state = change(state, 'diary_save', diary(member_id))
        assert_error(429, change, state, 'diary_save', diary(member_id))
        for _ in range(10):
            state = change(state, 'medication_save', medication(member_id))
        assert_error(429, change, state, 'medication_save', medication(member_id))
        state = change(state, 'diary_delete', {'id': state['diaries'][0]['id']})
        state = change(state, 'diary_save', diary(member_id))
        assert len(state['diaries']) == 20


def test_max_serialized_bytes_is_utf8_size_and_atomic(guest_app):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        member_id = state['members'][0]['id']
        payload = diary(member_id, symptoms='示' * 500, notes='例' * 1000)
        while True:
            try:
                state = change(state, 'diary_save', payload)
            except GuestExperienceError as error:
                assert error.status_code == 429
                break
        assert len(state['diaries']) < 20
        assert len(json.dumps(state, ensure_ascii=False).encode()) < 32768
        assert get_experience(GUEST_A) == state


def test_global_capacity_500_reclaims_only_expired_slots(guest_app, monkeypatch):
    now = [1_800_000_000]
    monkeypatch.setattr(experience.time, 'time', lambda: now[0])
    with guest_app.app_context():
        for number in range(500):
            get_experience(f'guest:{number:016d}')
        assert_error(429, get_experience, GUEST_A)
        assert get_experience('guest:0000000000000000')
        now[0] += 7200
        assert get_experience(GUEST_A)
        assert len(guest_app.extensions['guest_experience_store']._states) == 1


def test_concurrent_creation_is_idempotent_and_cas_has_one_winner(guest_app):
    def create():
        with guest_app.app_context():
            return get_experience(GUEST_A)
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda _: create(), range(16)))
    assert all(state == states[0] for state in states)
    barrier = threading.Barrier(8)
    def edit(number):
        with guest_app.app_context():
            barrier.wait(timeout=5)
            try:
                return change(states[0], 'profile', {'age': 65 + number})['version']
            except GuestExperienceError as error:
                return error.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(edit, range(8)))
    assert results.count(409) == 7
    assert results.count(states[0]['version'] + 1) == 1


def test_production_without_redis_fails_closed_and_ignores_injected_memory(guest_app):
    guest_app.config.update(TESTING=False, GUEST_EXPERIENCE_STORE=experience.GuestExperienceMemoryStore())
    with guest_app.app_context():
        assert_error(503, get_experience, GUEST_A)
        assert 'guest_experience_store' not in guest_app.extensions


def test_memory_backend_cannot_survive_switch_to_production(guest_app):
    with guest_app.app_context():
        get_experience(GUEST_A)
        guest_app.config['TESTING'] = False
        assert_error(503, get_experience, GUEST_A)


def test_debug_without_redis_allows_bounded_local_store(guest_app):
    guest_app.config.update(TESTING=False, DEBUG=True)
    with guest_app.app_context():
        assert get_experience(GUEST_A)


def test_redis_failure_does_not_fall_back_or_expose_internal_error(guest_app):
    class BrokenRedis:
        def eval(self, *args):
            raise RuntimeError('redis://secret:password@private-host failure')
    guest_app.config['GUEST_EXPERIENCE_REDIS_CLIENT'] = BrokenRedis()
    with guest_app.app_context():
        error = assert_error(503, get_experience, GUEST_A)
        assert 'secret' not in str(error)
        assert 'private-host' not in str(error)
        assert isinstance(guest_app.extensions['guest_experience_store'], experience._RedisStore)


def test_service_has_no_real_data_or_notification_imports():
    tree = ast.parse(inspect.getsource(experience))
    imports = [node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert set(imports) <= {'__future__', 'datetime', 'flask', 'core.constants'}
    assert not any(word in module for module in imports for word in ('db_models', 'auth', 'notification'))


@pytest.fixture
def redis_app(tmp_path):
    binary = shutil.which('redis-server')
    if binary is None:
        pytest.skip('未安装 redis-server，真实 Redis 原子脚本需在有该工具的环境执行')
    redis = pytest.importorskip('redis')
    # 仅启动本用例专用进程，不读取宿主配置，也不监听 TCP 或写入持久化文件。
    socket_path = str(tmp_path / 'redis.sock')
    process = subprocess.Popen([
        binary, '--port', '0', '--save', '', '--appendonly', 'no',
        '--unixsocket', socket_path, '--unixsocketperm', '700',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = redis.Redis(unix_socket_path=socket_path, decode_responses=True, socket_timeout=1)
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except redis.RedisError:
                time.sleep(0.02)
        else:
            pytest.fail('测试专用 Redis 未就绪')
        app = Flask(__name__)
        app.config.update(TESTING=True, GUEST_EXPERIENCE_REDIS_CLIENT=client)
        yield app, client
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)


def test_real_redis_preserves_schema_fixed_ttl_and_cas(redis_app):
    app, client = redis_app
    with app.app_context():
        state = get_experience(GUEST_A)
        key = experience._key(GUEST_A)
        assert isinstance(state['diaries'], list)
        assert isinstance(state['medications'], list)
        assert isinstance(state['profile']['chronic_diseases'], list)
        initial_ttl = client.pttl(key)
        state = change(state, 'profile', {'age': 75})
        version = state['version']
        expires_at = state['expires_at']
        state = change(state, 'reset', {})
        assert state['version'] == version + 1
        assert state['expires_at'] == expires_at
        assert client.pttl(key) <= initial_ttl
        assert_error(409, mutate_experience, GUEST_A, 'profile', {'age': 60}, version)
        client.pexpire(key, 1)
        time.sleep(0.01)
        assert_error(409, change, state, 'reset', {})


def test_real_redis_capacity_is_atomic_under_parallel_creation(redis_app, monkeypatch):
    app, client = redis_app
    monkeypatch.setattr(experience, 'MAX_ACTIVE_EXPERIENCES', 3)
    def create(number):
        with app.app_context():
            try:
                return get_experience(f'guest:{number:016d}')['schema_version']
            except GuestExperienceError as error:
                return error.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(16)))
    assert results.count(1) == 3
    assert results.count(429) == 13
    assert client.zcard(f'{experience._NAMESPACE}:active') == 3


def test_real_redis_concurrent_cas_has_one_winner(redis_app):
    app, _ = redis_app
    with app.app_context():
        state = get_experience(GUEST_A)
    barrier = threading.Barrier(8)
    def edit(number):
        with app.app_context():
            barrier.wait(timeout=5)
            try:
                return change(state, 'profile', {'age': 65 + number})['version']
            except GuestExperienceError as error:
                return error.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(edit, range(8)))
    assert results.count(409) == 7
    assert results.count(state['version'] + 1) == 1


def test_assessment_is_temporary_and_profile_reset_invalidate_it(guest_app):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        assessment = {'assessment_date': '2026-10-04T12:00:00', 'risk_score': 2,
                      'risk_level': '示例', 'recommendations': '[]', 'explain': '{}'}
        saved = experience.save_guest_assessment(GUEST_A, assessment, state['version'])
        assert saved['assessment'] == assessment
        assert saved['expires_at'] == state['expires_at']
        assert saved['version'] == state['version'] + 1
        assert get_experience(GUEST_B)['assessment'] is None
        saved = change(saved, 'profile', {'age': 75})
        assert saved['assessment'] is None
        saved = experience.save_guest_assessment(GUEST_A, assessment, saved['version'])
        assert change(saved, 'reset', {})['assessment'] is None


def test_assessment_captured_version_prevents_stale_computation_write(guest_app):
    with guest_app.app_context():
        initial = get_experience(GUEST_A)
        current = change(initial, 'profile', {'age': 80})
        assert_error(409, experience.save_guest_assessment, GUEST_A, {'risk_score': 1}, initial['version'])
        assert get_experience(GUEST_A) == current
        assert_error(400, experience.save_guest_assessment, GUEST_A, [], current['version'])
        assert_error(400, experience.save_guest_assessment, GUEST_A, {}, True)
        assert_error(429, experience.save_guest_assessment, GUEST_A, {'explain': 'x' * 32768}, current['version'])
        assert_error(400, experience.save_guest_assessment, GUEST_A, {'explain': float('nan')}, current['version'])
        assert get_experience(GUEST_A) == current


def test_assessment_default_retries_once_without_losing_parallel_checkin(guest_app, monkeypatch):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        member_id = state['members'][0]['id']
        store = guest_app.extensions['guest_experience_store']
        real_cas = store.cas
        calls = []
        def racing_cas(key, version, raw):
            calls.append(version)
            if len(calls) == 1:
                competing = json.loads(store.get(key))
                competing['checkins'][member_id] = {'actions': ['rest'], 'completed_at': int(time.time()), 'reviewed': False}
                real_cas(key, version, experience._encode(competing))
            return real_cas(key, version, raw)
        monkeypatch.setattr(store, 'cas', racing_cas)
        saved = experience.save_guest_assessment(GUEST_A, {'risk_score': 1})
        assert len(calls) == 2
        assert saved['checkins'][member_id]['actions'] == ['rest']
        assert saved['assessment']['risk_score'] == 1
        assert saved['version'] == state['version'] + 2


def test_assessment_explicit_version_never_retries_cas(guest_app, monkeypatch):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        store = guest_app.extensions['guest_experience_store']
        calls = []
        def conflict(*args):
            calls.append(args)
            raise experience._stale()
        monkeypatch.setattr(store, 'cas', conflict)
        assert_error(409, experience.save_guest_assessment, GUEST_A, {}, state['version'])
        assert len(calls) == 1
        calls.clear()
        assert_error(409, experience.save_guest_assessment, GUEST_A, {})
        assert len(calls) == 2


def test_delete_releases_capacity_and_expired_assessment_is_not_recreated(guest_app, monkeypatch):
    monkeypatch.setattr(experience, 'MAX_ACTIVE_EXPERIENCES', 1)
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        assert_error(429, get_experience, GUEST_B)
        assert experience.delete_experience(GUEST_A) is True
        assert experience.delete_experience(GUEST_A) is False
        assert get_experience(GUEST_A, create=False) is None
        assert_error(409, experience.save_guest_assessment, GUEST_A, {}, state['version'])
        assert get_experience(GUEST_B)


def test_real_redis_delete_and_assessment(redis_app, monkeypatch):
    app, client = redis_app
    monkeypatch.setattr(experience, 'MAX_ACTIVE_EXPERIENCES', 1)
    with app.app_context():
        state = get_experience(GUEST_A)
        saved = experience.save_guest_assessment(GUEST_A, {'recommendations': []}, state['version'])
        assert saved['assessment']['recommendations'] == []
        assert saved['expires_at'] == state['expires_at']
        assert_error(409, experience.save_guest_assessment, GUEST_A, {}, state['version'])
        assert experience.delete_experience(GUEST_A)
        assert client.zcard(f'{experience._NAMESPACE}:active') == 0
        assert get_experience(GUEST_B)


def test_configured_production_redis_failure_has_no_memory_fallback(guest_app, monkeypatch):
    redis = pytest.importorskip('redis')
    class BrokenRedis:
        def eval(self, *args):
            raise redis.ConnectionError('private connection details')
    seen_urls = []
    def client_from_url(url, **kwargs):
        seen_urls.append(url)
        return BrokenRedis()
    monkeypatch.setattr(redis.Redis, 'from_url', client_from_url)
    guest_app.config.update(TESTING=False, WEATHER_CACHE_REDIS_URL='redis://localhost:1/9')
    with guest_app.app_context():
        assert_error(503, get_experience, GUEST_A)
        assert seen_urls == ['redis://localhost:1/9']
        assert isinstance(guest_app.extensions['guest_experience_store'], experience._RedisStore)


def test_assessment_retry_rejects_parallel_profile_change(guest_app, monkeypatch):
    with guest_app.app_context():
        state = get_experience(GUEST_A)
        store = guest_app.extensions['guest_experience_store']
        real_cas = store.cas
        calls = []
        def racing_cas(key, version, raw):
            calls.append(version)
            competing = json.loads(store.get(key))
            competing['profile']['age'] = 90
            real_cas(key, version, experience._encode(competing))
            return real_cas(key, version, raw)
        monkeypatch.setattr(store, 'cas', racing_cas)
        assert_error(409, experience.save_guest_assessment, GUEST_A, {'risk_score': 1})
        assert len(calls) == 1
        assert get_experience(GUEST_A)['assessment'] is None
        assert get_experience(GUEST_A)['version'] == state['version'] + 1
