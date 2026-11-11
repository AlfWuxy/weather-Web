"""跨进程资源预算；数据库不可用时禁止继续付费调用。"""
import hashlib
import secrets
import threading
import time
from collections import OrderedDict
from datetime import timedelta

import sqlalchemy as sa
from flask import current_app, g, has_app_context, has_request_context
from flask_login import current_user
from sqlalchemy import delete, update
from sqlalchemy.exc import SQLAlchemyError

from core.extensions import db
from core.time_utils import utcnow


class ResourceBudget(db.Model):
    __tablename__ = 'resource_budgets'
    key = db.Column(db.String(180), primary_key=True)
    used = db.Column(db.BigInteger, nullable=False, default=0)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)


class ResourceLease(db.Model):
    __tablename__ = 'resource_leases'
    key = db.Column(db.String(80), primary_key=True)
    owner = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)


class ResourceLimitError(ValueError):
    pass


class ResourceBudgetUnavailable(RuntimeError):
    pass


class UpstreamBusy(RuntimeError):
    pass


_UPSTREAM_LIMIT_DEFAULTS = {
    'WEATHER': (300, 2000, 40000),
    'MAP': (200, 1000, 15000),
}
_CACHE_EXT_KEY = 'upstream_ttl_cache'
_INFLIGHT_EXT_KEY = 'upstream_inflight_semaphores'
_process_guard = threading.Lock()
_process_cache = None
_process_inflight = {}


def setting(name, default):
    """零值代表关闭服务，不能通过负数或坏值取消限制。"""
    try:
        return max(0, int(current_app.config.get(name, default)))
    except (TypeError, ValueError):
        return default


def actor_id():
    if has_request_context():
        if getattr(g, 'api_user_id', None):
            return str(g.api_user_id)
        if current_user.is_authenticated:
            return str(current_user.id)
    return 'background'


def _insert_ignore(conn, table, values):
    if conn.dialect.name == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    elif conn.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise ResourceBudgetUnavailable('资源预算数据库类型不受支持')
    conn.execute(insert(table).values(**values).on_conflict_do_nothing())


def _table_ready(table_name):
    """探测预算表。成功返回布尔值；数据库不可读时返回 None。"""
    try:
        inspector = sa.inspect(db.engine)
        clear_cache = getattr(inspector, 'clear_cache', None)
        if clear_cache:
            clear_cache()
        return bool(inspector.has_table(table_name))
    except Exception:
        return None


def _stable_slot(value, modulo):
    if isinstance(value, bytes):
        raw = value
    else:
        raw = str(value).encode('utf-8')
    return int(hashlib.sha256(raw).hexdigest(), 16) % modulo


def _apply_budget_limits(rows, now):
    """一个短事务内清理过期行并原子扣减；任一行超限则整单回滚。"""
    table = ResourceBudget.__table__
    try:
        with db.engine.begin() as conn:
            conn.execute(delete(table).where(table.c.expires_at < now))
            for key, amount, limit, expires_at in rows:
                _insert_ignore(conn, table, {'key': key, 'used': 0, 'expires_at': expires_at})
                result = conn.execute(update(table).where(
                    table.c.key == key, table.c.used <= limit - amount,
                ).values(used=table.c.used + amount))
                if result.rowcount != 1:
                    raise ResourceLimitError('已达到服务使用额度，请稍后再试')
    except SQLAlchemyError as exc:
        raise ResourceBudgetUnavailable('资源预算暂不可用') from exc


def reserve(resource, *, units=1, user_id=None):
    """所有额度在一个短事务内原子预留；超时与重试仍计费，不退款。"""
    if not isinstance(units, int) or units < 1:
        raise ResourceLimitError('资源预留必须为正整数')
    now = utcnow()
    day, month = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m')
    who = str(user_id) if user_id is not None else actor_id()
    defaults = {'AI': (60, 300, 5000), 'GEOCODE': (20, 200, 3000)}
    user_limit, daily, monthly = defaults[resource]
    limits = [
        (f'{resource}:user:{who}:{day}', 1, setting(f'{resource}_USER_DAILY_LIMIT', user_limit)),
        (f'{resource}:day:{day}', 1, setting(f'{resource}_DAILY_LIMIT', daily)),
        (f'{resource}:month:{month}', 1, setting(f'{resource}_MONTHLY_LIMIT', monthly)),
    ]
    if resource == 'AI':
        limits.extend([
            (f'AI:tokens:user:{who}:{day}', units, setting('AI_USER_DAILY_TOKEN_LIMIT', 200000)),
            (f'AI:tokens:day:{day}', units, setting('AI_DAILY_TOKEN_LIMIT', 1200000)),
            (f'AI:tokens:month:{month}', units, setting('AI_MONTHLY_TOKEN_LIMIT', 20000000)),
        ])
    table = ResourceBudget.__table__
    try:
        with db.engine.begin() as conn:
            conn.execute(delete(table).where(table.c.expires_at < now))
            for key, amount, limit in limits:
                _insert_ignore(conn, table, {'key': key, 'used': 0, 'expires_at': now + timedelta(days=40)})
                result = conn.execute(update(table).where(
                    table.c.key == key, table.c.used <= limit - amount,
                ).values(used=table.c.used + amount))
                if result.rowcount != 1:
                    raise ResourceLimitError('已达到服务使用额度，请稍后再试')
    except SQLAlchemyError as exc:
        raise ResourceBudgetUnavailable('资源预算暂不可用') from exc


def reserve_registration(ip_hash: str):
    """全局注册日额度，外加按来源散列的分片额度，避免单桶打满。"""
    now = utcnow()
    day = now.strftime('%Y-%m-%d')
    shard_count = setting('REGISTER_SHARD_COUNT', 16)
    if shard_count < 1:
        raise ResourceLimitError('已达到服务使用额度，请稍后再试')
    slot = _stable_slot(ip_hash, shard_count)
    expires_at = now + timedelta(days=40)
    _apply_budget_limits([
        (f'REGISTER:day:{day}', 1, setting('REGISTER_DAILY_LIMIT', 200), expires_at),
        (
            f'REGISTER:shard:{slot}:{day}',
            1,
            setting('REGISTER_SHARD_DAILY_LIMIT', 40),
            expires_at,
        ),
    ], now)


def reserve_upstream(resource: str, units: int = 1):
    """天气/地图上游调用的小时、日、月额度。无应用或测试库缺表时放行。"""
    resource = str(resource or '').strip().upper()
    if resource not in _UPSTREAM_LIMIT_DEFAULTS:
        raise ResourceLimitError('不支持的上游资源')
    if not isinstance(units, int) or units < 1:
        raise ResourceLimitError('资源预留必须为正整数')
    if not has_app_context():
        return
    testing = bool(current_app.config.get('TESTING'))
    ready = _table_ready('resource_budgets')
    if ready is not True:
        if testing:
            return
        raise ResourceBudgetUnavailable('资源预算暂不可用')
    now = utcnow()
    hourly_default, daily_default, monthly_default = _UPSTREAM_LIMIT_DEFAULTS[resource]
    hour = now.strftime('%Y-%m-%dT%H')
    day = now.strftime('%Y-%m-%d')
    month = now.strftime('%Y-%m')
    _apply_budget_limits([
        (
            f'{resource}:hour:{hour}',
            units,
            setting(f'{resource}_UPSTREAM_HOURLY_LIMIT', hourly_default),
            now + timedelta(days=2),
        ),
        (
            f'{resource}:day:{day}',
            units,
            setting(f'{resource}_UPSTREAM_DAILY_LIMIT', daily_default),
            now + timedelta(days=40),
        ),
        (
            f'{resource}:month:{month}',
            units,
            setting(f'{resource}_UPSTREAM_MONTHLY_LIMIT', monthly_default),
            now + timedelta(days=40),
        ),
    ], now)


def acquire_lease(query, ttl_seconds=15, namespace='geocode'):
    """固定散列槽限制锁表大小，同时合并跨 worker 的相同查询。"""
    if not namespace:
        namespace = 'geocode'
    namespace = str(namespace)
    if not has_app_context() or (
        current_app.config.get('TESTING') and _table_ready('resource_leases') is not True
    ):
        return ('memory:' + namespace, 'testing')
    slot = int(hashlib.sha256(query.encode()).hexdigest(), 16) % 256
    lock_key = f'{namespace}:{slot}'
    owner = secrets.token_hex(16)
    now = utcnow()
    table = ResourceLease.__table__
    try:
        with db.engine.begin() as conn:
            _insert_ignore(conn, table, {'key': lock_key, 'owner': '', 'expires_at': now - timedelta(seconds=1)})
            result = conn.execute(update(table).where(
                table.c.key == lock_key, table.c.expires_at < now,
            ).values(owner=owner, expires_at=now + timedelta(seconds=ttl_seconds)))
            return (lock_key, owner) if result.rowcount == 1 else None
    except SQLAlchemyError as exc:
        raise ResourceBudgetUnavailable('资源预算暂不可用') from exc


def release_lease(lease):
    if not lease or str(lease[0]).startswith('memory:'):
        return
    table = ResourceLease.__table__
    try:
        with db.engine.begin() as conn:
            conn.execute(update(table).where(table.c.key == lease[0], table.c.owner == lease[1])
                         .values(expires_at=utcnow() - timedelta(seconds=1)))
    except SQLAlchemyError:
        pass


class UpstreamTTLCache:
    """进程内短 TTL 缓存，调用方按应用扩展隔离。"""

    def __init__(self, maxsize=256):
        try:
            size = int(maxsize)
        except (TypeError, ValueError):
            size = 256
        self.maxsize = size if size > 0 else 256
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return default
            value, expires_at = item
            if expires_at <= now:
                self._items.pop(key, None)
                return default
            self._items.move_to_end(key)
            return value

    def set(self, key, value, ttl_seconds):
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError):
            ttl = 0
        now = time.monotonic()
        with self._lock:
            if ttl <= 0:
                self._items.pop(key, None)
                return
            self._items[key] = (value, now + ttl)
            self._items.move_to_end(key)
            expired = [item_key for item_key, (_, exp) in self._items.items() if exp <= now]
            for item_key in expired:
                self._items.pop(item_key, None)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)


def get_upstream_cache():
    """返回当前应用独占的上游 TTL 缓存。"""
    global _process_cache
    if not has_app_context():
        with _process_guard:
            if _process_cache is None:
                _process_cache = UpstreamTTLCache(maxsize=256)
            return _process_cache
    with _process_guard:
        cache = current_app.extensions.get(_CACHE_EXT_KEY)
        if not isinstance(cache, UpstreamTTLCache):
            cache = UpstreamTTLCache(maxsize=256)
            current_app.extensions[_CACHE_EXT_KEY] = cache
        return cache


def _inflight_semaphore(resource):
    limit = 6
    if has_app_context():
        limit = setting(f'{resource}_MAX_INFLIGHT', 6)
    if limit < 1:
        return None
    with _process_guard:
        if has_app_context():
            store = current_app.extensions.get(_INFLIGHT_EXT_KEY)
            if not isinstance(store, dict):
                store = {}
                current_app.extensions[_INFLIGHT_EXT_KEY] = store
        else:
            store = _process_inflight
        current = store.get(resource)
        if not isinstance(current, tuple) or current[0] != limit:
            current = (limit, threading.BoundedSemaphore(limit))
            store[resource] = current
        return current[1]


class _InflightSlot:
    def __init__(self, resource, timeout):
        self._resource = str(resource or '').strip().upper()
        self._timeout = 2.0 if timeout is None else timeout
        self._semaphore = None
        self._held = False

    def __enter__(self):
        semaphore = _inflight_semaphore(self._resource)
        if semaphore is None:
            raise UpstreamBusy('上游并发已关闭')
        try:
            timeout = float(self._timeout)
        except (TypeError, ValueError):
            timeout = 2.0
        if timeout < 0:
            timeout = 0.0
        self._semaphore = semaphore
        self._held = bool(semaphore.acquire(timeout=timeout))
        if not self._held:
            raise UpstreamBusy('上游调用繁忙，请稍后再试')
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._held and self._semaphore is not None:
            self._semaphore.release()
            self._held = False
        return False


def upstream_inflight_slot(resource, timeout=2.0):
    """进程内有界并发。排队超时抛出 UpstreamBusy，不再打向上游。"""
    return _InflightSlot(resource, timeout)
