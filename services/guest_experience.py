# -*- coding: utf-8 -*-
"""游客体验专用临时状态；不连接真实账户、业务数据库或通知服务。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from datetime import date

from flask import current_app

from core.constants import CHRONIC_OPTIONS, DEFAULT_CITY_LABEL

TTL_SECONDS = 7200
MAX_ACTIVE_EXPERIENCES = 500
MAX_STATE_BYTES = 32 * 1024
MAX_MEMBERS = 3
MAX_DIARIES = 20
MAX_MEDICATIONS = 10
ACTIONS = ('drink_water', 'rest', 'contact')
_NAMESPACE = '{guest-experience:v1}'
_EXTENSION_KEY = 'guest_experience_store'
_BACKEND_LOCK = threading.Lock()
_GUEST_ID = re.compile(r'guest:[A-Za-z0-9_-]{12,128}\Z')


class GuestExperienceError(Exception):
    """可直接展示给用户的业务错误，不包含后端连接信息。"""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _unavailable():
    return GuestExperienceError('体验服务暂时不可用，请稍后再试。', 503)


def _stale():
    return GuestExperienceError('体验内容已更新或过期，请刷新页面后重试。', 409)


def _quota():
    return GuestExperienceError('体验空间已满，请删除部分体验记录后重试。', 429)


def _key(guest_id):
    if not isinstance(guest_id, str) or not _GUEST_ID.fullmatch(guest_id):
        raise GuestExperienceError('请重新进入游客体验。', 400)
    # 仅接受登录层生成的游客标识，缓存键不保留原始标识。
    return f'{_NAMESPACE}:state:{hashlib.sha256(guest_id.encode()).hexdigest()}'


def _demo_id(kind):
    return f'demo_{kind}_{secrets.token_urlsafe(12)}'


def _seed(now):
    return {
        'schema_version': 1,
        # 新一轮体验采用独立版本，防止过期页面覆盖重新生成的体验。
        'version': secrets.randbits(48) + 1,
        'expires_at': int(now) + TTL_SECONDS,
        'profile': {
            'username': '示例长者', 'age': 65, 'gender': '未知',
            'community': DEFAULT_CITY_LABEL, 'has_chronic_disease': False,
            'chronic_diseases': [],
        },
        'members': [
            {'id': _demo_id('member'), 'name': '示例家人', 'age': 72, 'relationship': '家人'}
        ],
        'checkins': {}, 'diaries': [], 'medications': [], 'assessment': None,
    }


def _encode(state):
    try:
        raw = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        size = len(raw.encode('utf-8'))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GuestExperienceError('提交内容格式不正确，请检查后重试。') from None
    if size > MAX_STATE_BYTES:
        raise _quota()
    return raw


def _decode(raw):
    try:
        if not isinstance(raw, (str, bytes)) or len(raw if isinstance(raw, bytes) else raw.encode('utf-8')) > MAX_STATE_BYTES:
            raise ValueError('invalid size')
        state = json.loads(raw)
        if (not isinstance(state, dict) or state.get('schema_version') != 1
                or type(state.get('version')) is not int or state['version'] <= 0
                or type(state.get('expires_at')) is not int
                or not isinstance(state.get('profile'), dict)
                or not isinstance(state.get('members'), list)
                or not isinstance(state.get('checkins'), dict)
                or not isinstance(state.get('diaries'), list)
                or not isinstance(state.get('medications'), list)):
            raise ValueError('invalid schema')
        return state
    except (ValueError, TypeError, UnicodeError):
        raise _unavailable() from None


# 两个键使用相同哈希槽；容量预留与状态创建必须在同一个原子脚本中完成。
# 只替换固定数字字段，避免 Lua 将空 JSON 数组重新编码成对象。
_GET_LUA = """
local now = tonumber(redis.call('TIME')[1])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local raw = redis.call('GET', KEYS[1])
if raw then return {1, raw} end
if ARGV[1] == '' then return {0, ''} end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then return {429, ''} end
local expires_at = now + tonumber(ARGV[2])
raw = string.gsub(ARGV[1], '"expires_at":%d+', '"expires_at":' .. expires_at, 1)
if string.len(raw) > tonumber(ARGV[4]) then return {429, ''} end
redis.call('SET', KEYS[1], raw, 'EXAT', expires_at)
redis.call('ZADD', KEYS[2], expires_at, KEYS[1])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
return {1, raw}
"""

_CAS_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {409, ''} end
local current = cjson.decode(raw)
local now = tonumber(redis.call('TIME')[1])
if current.expires_at <= now or current.version ~= tonumber(ARGV[1]) then
    return {409, ''}
end
raw = string.gsub(ARGV[2], '"expires_at":%d+', '"expires_at":' .. current.expires_at, 1)
if string.len(raw) > tonumber(ARGV[3]) then return {429, ''} end
redis.call('SET', KEYS[1], raw, 'EXAT', current.expires_at)
return {1, raw}
"""


_DELETE_LUA = """
local removed = redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], KEYS[1])
return {1, removed}
"""


class GuestExperienceMemoryStore:
    """仅供显式测试或本地调试使用的有界后端，行为与 Redis 保持一致。"""

    def __init__(self):
        self._states = {}
        self._lock = threading.Lock()

    def get(self, key, seed=None):
        with self._lock:
            now = int(time.time())
            expired = [key for key, (expires, _) in self._states.items() if expires <= now]
            for expired_key in expired:
                del self._states[expired_key]
            if key in self._states:
                return self._states[key][1]
            if seed is None:
                return None
            if len(self._states) >= MAX_ACTIVE_EXPERIENCES:
                raise _quota()
            state = _decode(seed)
            state['expires_at'] = now + TTL_SECONDS
            raw = _encode(state)
            self._states[key] = (state['expires_at'], raw)
            return raw

    def cas(self, key, expected_version, raw):
        with self._lock:
            entry = self._states.get(key)
            if entry is None or entry[0] <= int(time.time()):
                self._states.pop(key, None)
                raise _stale()
            current = _decode(entry[1])
            if current['version'] != expected_version:
                raise _stale()
            state = _decode(raw)
            state['version'] = current['version'] + 1
            state['expires_at'] = current['expires_at']
            raw = _encode(state)
            self._states[key] = (state['expires_at'], raw)
            return raw

    def delete(self, key):
        with self._lock:
            return self._states.pop(key, None) is not None


class _RedisStore:
    def __init__(self, client):
        self.client = client

    def _eval(self, script, keys, args):
        try:
            code, raw = self.client.eval(script, len(keys), *keys, *args)
            if code == 429:
                raise _quota()
            if code == 409:
                raise _stale()
            if code == 0:
                return None
            if code != 1:
                raise _unavailable()
            return raw
        except GuestExperienceError:
            raise
        except Exception:
            # 不向网页或日志输出 Redis 地址、凭据或原始错误文本。
            raise _unavailable() from None

    def get(self, key, seed=None):
        return self._eval(_GET_LUA, [key, f'{_NAMESPACE}:active'], [
            seed or '', TTL_SECONDS, MAX_ACTIVE_EXPERIENCES, MAX_STATE_BYTES,
        ])

    def cas(self, key, expected_version, raw):
        return self._eval(_CAS_LUA, [key], [expected_version, raw, MAX_STATE_BYTES])

    def delete(self, key):
        return bool(self._eval(_DELETE_LUA, [key, f'{_NAMESPACE}:active'], []))


def _backend():
    app = current_app._get_current_object()
    testing = app.config.get('TESTING') is True
    local = testing or app.config.get('DEBUG') is True
    with _BACKEND_LOCK:
        if _EXTENSION_KEY in app.extensions:
            store = app.extensions[_EXTENSION_KEY]
            if isinstance(store, GuestExperienceMemoryStore) and not local:
                raise _unavailable()
            return store
        # 注入入口只对测试开放，正式环境不能借此启用进程内存储。
        injected = app.config.get('GUEST_EXPERIENCE_STORE')
        client = app.config.get('GUEST_EXPERIENCE_REDIS_CLIENT')
        if testing and injected is not None:
            store = injected
        elif testing and client is not None:
            store = _RedisStore(client)
        else:
            url = app.config.get('REDIS_URL') or app.config.get('WEATHER_CACHE_REDIS_URL')
            if url:
                try:
                    import redis
                    store = _RedisStore(redis.Redis.from_url(
                        url, decode_responses=True, socket_connect_timeout=1,
                        socket_timeout=1, retry_on_timeout=False,
                    ))
                except Exception:
                    raise _unavailable() from None
            elif local:
                store = GuestExperienceMemoryStore()
            else:
                raise _unavailable()
        app.extensions[_EXTENSION_KEY] = store
        return store


def get_experience(guest_id, create=True):
    """读取游客自己的体验；不存在且允许创建时，原子地预留容量并生成示例。"""
    key = _key(guest_id)
    raw = _backend().get(key, _encode(_seed(time.time())) if create else None)
    return _decode(raw) if raw is not None else None


def _payload(payload, allowed, required=()):
    if (not isinstance(payload, dict) or any(key not in allowed for key in payload)
            or any(key not in payload for key in required)):
        raise GuestExperienceError('提交内容不完整或格式不正确，请检查后重试。')


def _text(value, limit, required=True):
    if not isinstance(value, str):
        raise GuestExperienceError('请填写有效的文字内容。')
    value = value.strip()
    if (required and not value) or len(value) > limit or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise GuestExperienceError('文字内容为空、过长或包含无效字符，请检查后重试。')
    return value


def _age(value):
    if type(value) is not int or not 18 <= value <= 110:
        raise GuestExperienceError('体验年龄请填写 18 至 110 之间的整数。')
    return value


def _member(state, member_id):
    for member in state['members']:
        if isinstance(member_id, str) and member['id'] == member_id:
            return member
    raise GuestExperienceError('未找到这位示例家人，请刷新后重试。', 404)


def _record(records, record_id):
    for record in records:
        if isinstance(record_id, str) and record['id'] == record_id:
            return record
    raise GuestExperienceError('未找到这条体验记录，请刷新后重试。', 404)


def _profile(state, payload):
    allowed = {'username', 'age', 'gender', 'community', 'has_chronic_disease', 'chronic_diseases'}
    _payload(payload, allowed)
    state['assessment'] = None
    profile = state['profile']
    for key in ('username', 'community'):
        if key in payload:
            profile[key] = _text(payload[key], 30 if key == 'username' else 60)
    if 'age' in payload:
        profile['age'] = _age(payload['age'])
    if 'gender' in payload:
        if payload['gender'] not in ('男', '女', '未知'):
            raise GuestExperienceError('请选择有效的性别选项。')
        profile['gender'] = payload['gender']
    if 'has_chronic_disease' in payload:
        if type(payload['has_chronic_disease']) is not bool:
            raise GuestExperienceError('请选择是否存在慢性病。')
        profile['has_chronic_disease'] = payload['has_chronic_disease']
    if 'chronic_diseases' in payload:
        diseases = payload['chronic_diseases']
        if (not isinstance(diseases, list) or len(diseases) > len(CHRONIC_OPTIONS)
                or any(not isinstance(item, str) or item not in CHRONIC_OPTIONS for item in diseases)
                or len(set(diseases)) != len(diseases)):
            raise GuestExperienceError('请选择列表内的慢性病选项。')
        profile['chronic_diseases'] = diseases
    if not profile['has_chronic_disease']:
        profile['chronic_diseases'] = []


def _member_save(state, payload):
    _payload(payload, {'id', 'name', 'age', 'relationship'}, {'name', 'age', 'relationship'})
    record = _member(state, payload['id']) if 'id' in payload else None
    if record is None and len(state['members']) >= MAX_MEMBERS:
        raise _quota()
    member = {'id': record['id'] if record else _demo_id('member'),
              'name': _text(payload['name'], 30), 'age': _age(payload['age']),
              'relationship': _text(payload['relationship'], 20)}
    if record is None:
        state['members'].append(member)
    else:
        record.update(member)


def _diary_save(state, payload):
    _payload(payload, {'id', 'member_id', 'date', 'symptoms', 'severity', 'notes'},
             {'member_id', 'date', 'symptoms', 'severity'})
    _member(state, payload['member_id'])
    record = _record(state['diaries'], payload['id']) if 'id' in payload else None
    if record is None and len(state['diaries']) >= MAX_DIARIES:
        raise _quota()
    raw_date = payload['date']
    try:
        if not isinstance(raw_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw_date):
            raise ValueError('invalid date')
        date.fromisoformat(raw_date)
    except ValueError:
        raise GuestExperienceError('请选择有效日期。') from None
    if payload['severity'] not in ('轻微', '中等', '明显'):
        raise GuestExperienceError('请选择有效的症状程度。')
    diary = {'id': record['id'] if record else _demo_id('diary'),
             'member_id': payload['member_id'], 'date': raw_date,
             'symptoms': _text(payload['symptoms'], 500), 'severity': payload['severity'],
             'notes': _text(payload.get('notes', ''), 1000, required=False)}
    if record is None:
        state['diaries'].append(diary)
    else:
        record.update(diary)


def _medication_save(state, payload):
    _payload(payload, {'id', 'member_id', 'name', 'time', 'note'}, {'member_id', 'name', 'time'})
    _member(state, payload['member_id'])
    record = _record(state['medications'], payload['id']) if 'id' in payload else None
    if record is None and len(state['medications']) >= MAX_MEDICATIONS:
        raise _quota()
    raw_time = payload['time']
    if not isinstance(raw_time, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', raw_time):
        raise GuestExperienceError('用药时间请填写为有效的小时和分钟。')
    medication = {'id': record['id'] if record else _demo_id('medication'),
                  'member_id': payload['member_id'], 'name': _text(payload['name'], 80),
                  'time': raw_time, 'note': _text(payload.get('note', ''), 500, required=False)}
    if record is None:
        state['medications'].append(medication)
    else:
        record.update(medication)


def mutate_experience(guest_id, operation, payload, expected_version):
    """校验并原子保存一项体验操作；页面版本不一致时拒绝覆盖。"""
    key = _key(guest_id)
    if type(expected_version) is not int or not 0 < expected_version < 2 ** 53:
        raise GuestExperienceError('页面版本无效，请刷新后重试。')
    if not isinstance(operation, str) or not isinstance(payload, dict):
        raise GuestExperienceError('提交内容格式不正确，请检查后重试。')
    store = _backend()
    raw = store.get(key)
    if raw is None:
        raise _stale()
    state = _decode(raw)
    if state['version'] != expected_version:
        raise _stale()
    if operation == 'profile':
        _profile(state, payload)
    elif operation == 'member_save':
        _member_save(state, payload)
    elif operation == 'member_delete':
        _payload(payload, {'id'}, {'id'})
        member = _member(state, payload['id'])
        state['members'].remove(member)
        state['checkins'].pop(member['id'], None)
        for collection in ('diaries', 'medications'):
            state[collection] = [item for item in state[collection] if item['member_id'] != member['id']]
    elif operation == 'checkin':
        _payload(payload, {'member_id', 'actions'}, {'member_id', 'actions'})
        member = _member(state, payload['member_id'])
        actions = payload['actions']
        if (not isinstance(actions, list) or not 1 <= len(actions) <= len(ACTIONS)
                or any(not isinstance(action, str) or action not in ACTIONS for action in actions)
                or len(set(actions)) != len(actions)):
            raise GuestExperienceError('请至少选择一项有效的体验行动。')
        state['checkins'][member['id']] = {
            'actions': list(actions), 'completed_at': int(time.time()), 'reviewed': False,
        }
    elif operation == 'review':
        _payload(payload, {'member_id'}, {'member_id'})
        member = _member(state, payload['member_id'])
        checkin = state['checkins'].get(member['id'])
        if checkin is None:
            raise GuestExperienceError('请先完成这位示例家人的行动打卡。', 404)
        checkin['reviewed'] = True
    elif operation == 'diary_save':
        _diary_save(state, payload)
    elif operation == 'medication_save':
        _medication_save(state, payload)
    elif operation in ('diary_delete', 'medication_delete'):
        _payload(payload, {'id'}, {'id'})
        records = state['diaries' if operation == 'diary_delete' else 'medications']
        records.remove(_record(records, payload['id']))
    elif operation == 'reset':
        _payload(payload, set())
        state = _seed(time.time())
    else:
        raise GuestExperienceError('暂不支持这项体验操作。')
    # 重置与普通编辑均继承原到期时间，避免通过持续操作延长数据保留。
    state['version'] = expected_version + 1
    return _decode(store.cas(key, expected_version, _encode(state)))


def delete_experience(guest_id):
    """可信退出流程删除体验状态，并立即释放其容量名额。"""
    key = _key(guest_id)
    return _backend().delete(key)


def save_guest_assessment(guest_id, assessment, expected_version=None):
    """可信评估流程保存结果；计算前捕获版本可避免旧评估覆盖新画像。"""
    key = _key(guest_id)
    if not isinstance(assessment, dict):
        raise GuestExperienceError('体验评估内容格式不正确，请重新评估。')
    if expected_version is not None and (
            type(expected_version) is not int or not 0 < expected_version < 2 ** 53):
        raise GuestExperienceError('页面版本无效，请刷新后重试。')
    store = _backend()
    attempts = 1 if expected_version is not None else 2
    initial_profile = None
    initial_member_ids = None
    for attempt in range(attempts):
        raw = store.get(key)
        if raw is None:
            raise _stale()
        state = _decode(raw)
        if expected_version is not None and state['version'] != expected_version:
            raise _stale()
        if attempt and (state['profile'] != initial_profile or
                        [member['id'] for member in state['members']] != initial_member_ids):
            raise _stale()
        initial_profile = state['profile']
        initial_member_ids = [member['id'] for member in state['members']]
        version = state['version']
        state['assessment'] = assessment
        state['version'] = version + 1
        try:
            return _decode(store.cas(key, version, _encode(state)))
        except GuestExperienceError as error:
            if error.status_code != 409 or attempt + 1 >= attempts:
                raise
    raise _stale()
