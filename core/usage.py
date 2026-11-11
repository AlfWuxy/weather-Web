# -*- coding: utf-8 -*-
"""Pilot-loop helpers: API tokens + usage events.

These are product analytics (打开率/触发/反馈等) rather than security audit logs.
We store only minimal structured metadata; avoid PII.
"""

import json
import logging
import secrets
from contextlib import contextmanager
from datetime import timedelta

from flask import current_app
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy import inspect as sa_inspect

from core.analytics_privacy import sanitize_analytics_meta
from core.audit import _active_session, _transaction_has_database_writes
from core.constants import GUEST_ID_PREFIX
from core.db_models import ApiToken, UsageEvent, User
from core.extensions import db
from core.security import hash_identifier
from core.time_utils import utcnow

logger = logging.getLogger(__name__)

_API_TOKEN_QUOTA_MESSAGE = 'API Token 数量已达上限，请先撤销旧凭证后再生成'
_USAGE_EVENT_QUOTA_MESSAGE = '事件上报配额已满，请稍后再试'
_API_TOKEN_LOCK_KEY = 8715630102
_USAGE_EVENT_LOCK_KEY = 8715630101
_USAGE_MODELS = (ApiToken, UsageEvent)


class UsageQuotaError(Exception):
    pass


def _config_int(name, default):
    """读取整数配置；缺失或非法时回到默认值。"""
    try:
        return int(current_app.config.get(name, default))
    except (TypeError, ValueError, RuntimeError):
        return default


def _utc_day_start():
    """当日零点。与本模块 utcnow() 同一时区语义，避免和入库时间各算各的。"""
    now = utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class _UsageWriteSkipped(Exception):
    """日限额硬保护触发。事务回滚后按未写入处理，不记失败日志。"""


def _is_sqlite():
    try:
        return db.engine.dialect.name == 'sqlite'
    except Exception:
        return False


def _iter_pending(session):
    session = _active_session(session)
    for collection in (session.new, session.dirty, session.deleted):
        for obj in list(collection):
            yield obj


def _pending_models(session, types=_USAGE_MODELS):
    return [obj for obj in _iter_pending(session) if isinstance(obj, types)]


def _can_use_external_connection(session):
    """没有未提交的凭证/事件，且 SQLite 没有已经落到文件上的写入。"""
    session = _active_session(session)
    if getattr(session, '_flushing', False):
        return False
    if _pending_models(session):
        return False
    if _is_sqlite() and _transaction_has_database_writes(session):
        return False
    return True


def _release_clean_sqlite_read(session):
    """SQLite 只读事务会挡住同线程的另一条连接。

    与审计持久化相同，只 rollback 干净的只读事务来释放文件锁。
    有挂起对象、正在 flush，或已经落库的业务/审计写入时不动会话，
    避免 commit 提交半成品，也避免 rollback 带走审计行。
    """
    session = _active_session(session)
    if not _is_sqlite():
        return
    if session.new or session.dirty or session.deleted:
        return
    if getattr(session, '_flushing', False):
        return
    if _transaction_has_database_writes(session):
        return
    if not session.in_transaction():
        return
    try:
        db.session.rollback()
    except Exception as exc:
        logger.debug("释放 SQLite 只读事务失败，异常类型=%s", type(exc).__name__)


def _caller_transaction_open(session):
    """调用方事务里是否已有业务、审计或挂起对象。"""
    session = _active_session(session)
    if getattr(session, '_flushing', False):
        return True
    if any(True for _ in _iter_pending(session)):
        return True
    return _transaction_has_database_writes(session)


def _rowcount(result):
    value = getattr(result, 'rowcount', 0)
    if isinstance(value, int) and value > 0:
        return value
    return 0


def _inserted_id(result):
    pk = getattr(result, 'inserted_primary_key', None)
    if pk:
        return pk[0]
    return getattr(result, 'lastrowid', None)


def _acquire_quota_lock(conn, lock_key):
    dialect = getattr(conn.dialect, 'name', None)
    if dialect == 'sqlite':
        try:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        except Exception as exc:
            message = str(exc).lower()
            if 'within a transaction' not in message and 'cannot start' not in message:
                raise
        return
    if dialect == 'postgresql':
        conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(lock_key)})


@contextmanager
def _serial_core_connection(lock_key):
    """独立事务里先拿写锁，再做配额判断和写入，避免并发请求一起通过检查。"""
    with db.engine.begin() as conn:
        _acquire_quota_lock(conn, lock_key)
        yield conn


def _expire_persisted(model, user_id=None):
    session = _active_session(db.session)
    new_ids = {id(obj) for obj in session.new}
    for obj in list(session.identity_map.values()):
        if id(obj) in new_ids or not isinstance(obj, model):
            continue
        if user_id is not None:
            state = sa_inspect(obj)
            available = 'user_id' in state.dict or 'user_id' in state.committed_state
            current = state.dict.get('user_id', state.committed_state.get('user_id'))
            if available and current != user_id:
                continue
        session.expire(obj)


def _token_delete_statement(user_id, now, revoked_before):
    tokens = ApiToken.__table__
    expired = and_(tokens.c.expires_at.is_not(None), tokens.c.expires_at <= now)
    revoked = and_(tokens.c.revoked_at.is_not(None), tokens.c.revoked_at < revoked_before)
    stmt = tokens.delete().where(or_(expired, revoked))
    if user_id is not None:
        stmt = stmt.where(tokens.c.user_id == user_id)
    return stmt


def _event_delete_expired_statement(cutoff):
    events = UsageEvent.__table__
    return events.delete().where(and_(
        events.c.created_at.is_not(None),
        events.c.created_at < cutoff,
    ))


def _count_events(conn, *, user_id=None, created_from=None):
    events = UsageEvent.__table__
    stmt = select(func.count()).select_from(events)
    filters = []
    if user_id is not None:
        filters.append(events.c.user_id == user_id)
    if created_from is not None:
        filters.append(events.c.created_at >= created_from)
    if filters:
        stmt = stmt.where(and_(*filters))
    return int(conn.execute(stmt).scalar() or 0)


def _delete_oldest_on(conn, user_id, limit):
    """按创建时间、主键升序删除最旧的 limit 条事件。"""
    if limit <= 0:
        return 0
    events = UsageEvent.__table__
    id_stmt = (
        select(events.c.id)
        .where(events.c.user_id == user_id)
        .order_by(events.c.created_at.asc(), events.c.id.asc())
        .limit(limit)
    )
    oldest_ids = [row[0] for row in conn.execute(id_stmt)]
    if not oldest_ids:
        return 0
    result = conn.execute(events.delete().where(events.c.id.in_(oldest_ids)))
    return _rowcount(result)


def _insert_event(conn, values):
    result = conn.execute(UsageEvent.__table__.insert().values(**values))
    return _inserted_id(result)


def _materialize_usage_event(inserted_id, values):
    if inserted_id is not None:
        try:
            with db.session.no_autoflush:
                found = db.session.get(UsageEvent, inserted_id)
            if found is not None:
                return found
        except Exception:
            pass
    payload = dict(values)
    if inserted_id is not None:
        payload['id'] = inserted_id
    return UsageEvent(**payload)


def _rollback_if_isolated(caller_open):
    """用量失败时，不回滚调用方已有的业务或审计写入。

    caller_open 为假时，会话里只有这次用量操作自己打开的事务。
    rollback 用来释放 SQLite 只读锁，或清掉失败的孤立写入。
    """
    if caller_open:
        return
    session = _active_session(db.session)
    if session.new or session.dirty or session.deleted:
        return
    if getattr(session, '_flushing', False):
        return
    if not session.in_transaction():
        return
    try:
        db.session.rollback()
    except Exception as exc:
        logger.debug("用量失败后的会话释放失败，异常类型=%s", type(exc).__name__)


def _run_on_caller_session(session, operation):
    """在调用方事务里做清理。只使用保存点，不 commit。

    语句失败时回到保存点，避免外层业务或审计事务被标成失败。
    flush 进行中不能再开保存点；这时失败只记日志并返回 0。
    """
    session = _active_session(session)
    if getattr(session, '_flushing', False):
        try:
            with session.no_autoflush:
                return operation() or 0
        except Exception as exc:
            logger.debug("用量清理失败，异常类型=%s", type(exc).__name__)
            return 0
    try:
        with session.no_autoflush:
            with session.begin_nested():
                deleted = operation() or 0
        return deleted
    except Exception as exc:
        logger.debug("用量清理失败，异常类型=%s", type(exc).__name__)
        return 0


def _prune_tokens_on_session(session, user_id, now, revoked_before, commit):
    """删除留在调用方事务里。这里不 commit。

    会话已经有别的写入时才会走到这里。此时 commit=True 也不能提交，
    否则已 flush 的业务和审计会一起落盘。能单独提交的清理走独立短事务。
    """
    if commit:
        logger.debug("调用方事务中的凭证清理不提交会话")

    def _delete():
        result = session.connection().execute(
            _token_delete_statement(user_id, now, revoked_before)
        )
        return _rowcount(result)

    deleted = _run_on_caller_session(session, _delete)
    if deleted:
        _expire_persisted(ApiToken, user_id)
    return deleted


def prune_expired_api_tokens(user_id=None, commit=False):
    """删除已过期凭证，以及超过撤销保留期的旧撤销记录。

    保留期内（默认 7 天）刚撤销的记录必须留下，供审计追溯。
    调用方会话没有未提交写入时，删除走独立短事务并在该事务内提交，
    不触碰调用方会话。已有业务或审计写入时，删除留在调用方事务里，
    本函数不 commit。commit 不能用来把调用方的半成品一起提交。
    清理失败只记日志并返回 0，不拖垮主请求。
    """
    now = utcnow()
    retention_days = _config_int('API_TOKEN_REVOKED_RETENTION_DAYS', 7)
    if retention_days < 0:
        retention_days = 7
    revoked_before = now - timedelta(days=retention_days)
    session = _active_session(db.session)
    if not _can_use_external_connection(session):
        return _prune_tokens_on_session(session, user_id, now, revoked_before, commit)

    try:
        _release_clean_sqlite_read(session)
        with _serial_core_connection(_API_TOKEN_LOCK_KEY) as conn:
            result = conn.execute(_token_delete_statement(user_id, now, revoked_before))
            deleted = _rowcount(result)
    except Exception as exc:
        logger.debug("凭证清理失败，异常类型=%s", type(exc).__name__)
        return 0
    try:
        _expire_persisted(ApiToken, user_id)
    except Exception as exc:
        logger.debug("凭证清理刷新身份映射失败，异常类型=%s", type(exc).__name__)
    return deleted


def _token_quota_limits():
    max_active = _config_int('API_TOKEN_MAX_ACTIVE', 5)
    max_per_user = _config_int('API_TOKEN_MAX_PER_USER', 20)
    if max_active < 0:
        max_active = 5
    if max_per_user < 0:
        max_per_user = 20
    return max_active, max_per_user


def _count_api_tokens(conn, user_id, now):
    tokens = ApiToken.__table__
    active = select(func.count()).select_from(tokens).where(and_(
        tokens.c.user_id == user_id,
        tokens.c.revoked_at.is_(None),
        tokens.c.expires_at.is_not(None),
        tokens.c.expires_at > now,
    ))
    retained = select(func.count()).select_from(tokens).where(tokens.c.user_id == user_id)
    active_count = int(conn.execute(active).scalar() or 0)
    retained_count = int(conn.execute(retained).scalar() or 0)
    return active_count, retained_count


def _enforce_api_token_quota(conn, user_id, now, max_active, max_per_user):
    active_count, retained_count = _count_api_tokens(conn, user_id, now)
    if active_count >= max_active or retained_count >= max_per_user:
        raise UsageQuotaError(_API_TOKEN_QUOTA_MESSAGE)


def _api_token_values(user_id, name, token_hash, now):
    return {
        'user_id': user_id,
        'name': name,
        'token_hash': token_hash,
        'created_at': now,
        'expires_at': now + timedelta(days=api_token_ttl_days()),
    }


def _insert_api_token_row(conn, values):
    conn.execute(ApiToken.__table__.insert().values(**values))


def _sqlite_immediate_lock(conn):
    """BEGIN IMMEDIATE 必须成功。已经在事务里就失败，避免无写锁时继续插入。"""
    try:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
    except Exception as exc:
        message = str(exc).lower()
        if 'within a transaction' not in message and 'cannot start' not in message:
            raise
        logger.debug("凭证配额写锁没有拿到，异常类型=%s", type(exc).__name__)
        raise RuntimeError('SQLite 配额写锁没有拿到，已拒绝发放凭证') from exc


@contextmanager
def _locked_token_connection():
    """独立事务里先拿写锁，再计数和插入。SQLite 拿不到 IMMEDIATE 就失败关闭。"""
    dialect = getattr(db.engine.dialect, 'name', None)
    if dialect == 'sqlite':
        with db.engine.begin() as conn:
            _sqlite_immediate_lock(conn)
            yield conn
        return
    with _serial_core_connection(_API_TOKEN_LOCK_KEY) as conn:
        yield conn


def _require_existing_user(conn, user_id):
    users = User.__table__
    row = conn.execute(select(users.c.id).where(users.c.id == user_id)).first()
    if row is None:
        raise ValueError('existing user_id is required')


def _insert_token_external(user_id, values, now, max_active, max_per_user):
    with _locked_token_connection() as conn:
        _require_existing_user(conn, user_id)
        _enforce_api_token_quota(conn, user_id, now, max_active, max_per_user)
        _insert_api_token_row(conn, values)


def _insert_token_on_session(session, user_id, values, now, max_active, max_per_user):
    """外层事务的保存点里重新计数并插入。不提交调用方事务。"""
    session = _active_session(session)
    with session.no_autoflush:
        pending = _pending_models(session, (User, ApiToken))
        if pending:
            session.flush(pending)
        with session.begin_nested():
            connection = session.connection()
            _enforce_api_token_quota(connection, user_id, now, max_active, max_per_user)
            _insert_api_token_row(connection, values)


def create_api_token(user_id, name=None):
    """Create an API token for miniprogram binding.

    Returns the *plain token* (display once); only the hash is persisted.
    有效凭证或保留行数达到上限时抛出 UsageQuotaError。
    调用方事务空闲时，在同一条 BEGIN IMMEDIATE（或其他数据库的写锁）里计数并插入。
    SQLite 拿不到写锁则失败，不插入。
    调用方已有未提交写入时，在保存点里重新计数并插入，不提交调用方事务。
    """
    if not user_id:
        raise ValueError('existing user_id is required')

    session = _active_session(db.session)
    caller_open = _caller_transaction_open(session)
    now = utcnow()
    max_active, max_per_user = _token_quota_limits()
    plain = secrets.token_urlsafe(24)
    token_hash = hash_identifier(plain)
    values = _api_token_values(user_id, name, token_hash, now)
    try:
        if caller_open:
            if db.session.get(User, user_id) is None:
                raise ValueError('existing user_id is required')
            prune_expired_api_tokens(user_id=user_id, commit=False)
            _insert_token_on_session(
                session, user_id, values, now, max_active, max_per_user,
            )
        else:
            # 先放开只读事务，避免共享锁挡住本线程随后的 BEGIN IMMEDIATE。
            _release_clean_sqlite_read(session)
            prune_expired_api_tokens(user_id=user_id, commit=False)
            _release_clean_sqlite_read(session)
            _insert_token_external(user_id, values, now, max_active, max_per_user)
    except UsageQuotaError:
        _rollback_if_isolated(caller_open)
        raise
    return plain


def verify_api_token(plain_token):
    """Verify a plain token and return ApiToken row if valid (not revoked)."""
    if not plain_token:
        return None
    token_hash = hash_identifier(plain_token)
    if not token_hash:
        return None
    return ApiToken.query.join(User, User.id == ApiToken.user_id).filter(
        ApiToken.token_hash == token_hash,
        ApiToken.revoked_at.is_(None),
        ApiToken.expires_at.isnot(None),
        ApiToken.expires_at > utcnow(),
        # 旧 SQLite 数据若已复用账号 ID，早于当前账号创建时间的凭证仍不可复活。
        ApiToken.created_at.isnot(None),
        User.created_at.isnot(None),
        ApiToken.created_at >= User.created_at,
    ).first()


def api_token_ttl_days():
    """配置无效时使用有限默认值，禁止配置成永久凭证。"""
    try:
        return max(1, min(int(current_app.config.get('API_TOKEN_TTL_DAYS', 30)), 90))
    except (TypeError, ValueError):
        return 30


def revoke_api_tokens(user_id, token_id=None):
    """仅撤销指定所有者的凭证，由调用方与账号变更一起提交。"""
    query = ApiToken.query.filter_by(user_id=user_id, revoked_at=None)
    if token_id is not None:
        query = query.filter_by(id=token_id)
    return query.update({'revoked_at': utcnow()}, synchronize_session='fetch')


def _prune_events_on_session(session, cutoff, commit):
    """删除留在调用方事务里。这里不 commit。"""
    if commit:
        logger.debug("调用方事务中的事件清理不提交会话")

    def _delete():
        result = session.connection().execute(_event_delete_expired_statement(cutoff))
        return _rowcount(result)

    deleted = _run_on_caller_session(session, _delete)
    if deleted:
        _expire_persisted(UsageEvent)
    return deleted


def prune_expired_usage_events(commit=False):
    """删除超过保留期的历史使用事件。配置天数 <= 0 时不清理。

    会话空闲时走独立短事务并在该事务内提交。调用方已有未提交写入时，
    删除留在该事务内，不 commit。清理失败返回 0，不拖垮主请求。
    """
    retention_days = _config_int('USAGE_EVENT_RETENTION_DAYS', 90)
    if retention_days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=retention_days)
    session = _active_session(db.session)
    if not _can_use_external_connection(session):
        return _prune_events_on_session(session, cutoff, commit)

    try:
        _release_clean_sqlite_read(session)
        with _serial_core_connection(_USAGE_EVENT_LOCK_KEY) as conn:
            result = conn.execute(_event_delete_expired_statement(cutoff))
            deleted = _rowcount(result)
    except Exception as exc:
        logger.debug("事件清理失败，异常类型=%s", type(exc).__name__)
        return 0
    try:
        _expire_persisted(UsageEvent)
    except Exception as exc:
        logger.debug("事件清理刷新身份映射失败，异常类型=%s", type(exc).__name__)
    return deleted


def _delete_oldest_usage_events(user_id, limit):
    """按创建时间、主键升序删除最旧的 limit 条事件。"""
    if limit <= 0:
        return 0
    session = _active_session(db.session)
    if _can_use_external_connection(session):
        _release_clean_sqlite_read(session)
        with _serial_core_connection(_USAGE_EVENT_LOCK_KEY) as conn:
            return _delete_oldest_on(conn, user_id, limit)

    def _delete():
        return _delete_oldest_on(session.connection(), user_id, limit)

    return _run_on_caller_session(session, _delete)


def _usage_event_values(event_type, user_id, pair_id, member_id, source, meta):
    payload = None
    safe_meta = sanitize_analytics_meta(event_type, meta)
    if safe_meta is not None:
        payload = json.dumps(safe_meta, ensure_ascii=False)
    return {
        'user_id': user_id,
        'pair_id': pair_id,
        'member_id': member_id,
        'event_type': str(event_type)[:50],
        'meta_json': payload,
        'source': (str(source)[:20] if source else None),
        'created_at': utcnow(),
    }


def _write_usage_event_sql(
    conn,
    *,
    values,
    resolved_user_id,
    enforce_quota,
    retention_days,
    cutoff,
    day_start,
    global_daily_limit,
    daily_per_user,
    max_per_user,
):
    if retention_days > 0:
        conn.execute(_event_delete_expired_statement(cutoff))
    if enforce_quota:
        if global_daily_limit > 0:
            global_today = _count_events(conn, created_from=day_start)
            if global_today >= global_daily_limit:
                raise UsageQuotaError(_USAGE_EVENT_QUOTA_MESSAGE)
        if resolved_user_id is not None:
            if daily_per_user > 0:
                user_today = _count_events(
                    conn, user_id=resolved_user_id, created_from=day_start,
                )
                if user_today >= daily_per_user:
                    raise UsageQuotaError(_USAGE_EVENT_QUOTA_MESSAGE)
            if max_per_user > 0:
                user_total = _count_events(conn, user_id=resolved_user_id)
                if user_total >= max_per_user:
                    raise UsageQuotaError(_USAGE_EVENT_QUOTA_MESSAGE)
    else:
        if resolved_user_id is not None and max_per_user > 0:
            user_total = _count_events(conn, user_id=resolved_user_id)
            if user_total >= max_per_user:
                _delete_oldest_on(
                    conn,
                    resolved_user_id,
                    user_total - max_per_user + 1,
                )
        if global_daily_limit > 0:
            global_today = _count_events(conn, created_from=day_start)
            if global_today >= global_daily_limit * 2:
                raise _UsageWriteSkipped()
    return _insert_event(conn, values)


def _usage_write_kwargs(event_type, resolved_user_id, pair_id, member_id, source, meta):
    retention_days = _config_int('USAGE_EVENT_RETENTION_DAYS', 90)
    return {
        'values': _usage_event_values(
            event_type, resolved_user_id, pair_id, member_id, source, meta,
        ),
        'resolved_user_id': resolved_user_id,
        'enforce_quota': None,
        'retention_days': retention_days,
        'cutoff': utcnow() - timedelta(days=retention_days),
        'day_start': _utc_day_start(),
        'global_daily_limit': _config_int('USAGE_EVENT_GLOBAL_DAILY_LIMIT', 10000),
        'daily_per_user': _config_int('USAGE_EVENT_DAILY_PER_USER', 200),
        'max_per_user': _config_int('USAGE_EVENT_MAX_PER_USER', 1000),
    }


def _log_usage_external(kwargs):
    with _serial_core_connection(_USAGE_EVENT_LOCK_KEY) as conn:
        inserted_id = _write_usage_event_sql(conn, **kwargs)
    return _materialize_usage_event(inserted_id, kwargs['values'])


def _log_usage_on_session(session, kwargs):
    """写入留在调用方事务的保存点里。不 commit 调用方会话。"""
    with session.no_autoflush:
        pending_usage = _pending_models(session)
        if pending_usage:
            session.flush(pending_usage)
        connection = session.connection()
        with connection.begin_nested():
            inserted_id = _write_usage_event_sql(connection, **kwargs)
    return _materialize_usage_event(inserted_id, kwargs['values'])


def log_usage_event(
    event_type,
    user_id=None,
    pair_id=None,
    member_id=None,
    source='web',
    meta=None,
    *,
    enforce_quota=False,
):
    """Best-effort usage event logging (fail open).

    enforce_quota=True 时超限并抛出 UsageQuotaError。调用方事务里已有写入时不回滚它。
    内部写入（默认）在单用户存量达到上限时按 FIFO 淘汰最旧事件；
    全站当日达到日限额 2 倍硬保护时放弃写入并返回 None。
    会话空闲时，清理、计数和插入在同一条独立短事务里提交。
    调用方已有业务或审计写入时，事件留在该事务的保存点里，不 commit。
    """
    if not event_type:
        return None
    # 游客只有会话标识，不是 users 表主键；保留匿名事件而不制造孤儿引用。
    if isinstance(user_id, str) and user_id.startswith(GUEST_ID_PREFIX):
        user_id = None
    resolved_user_id = user_id
    caller_open = False
    try:
        session = _active_session(db.session)
        caller_open = _caller_transaction_open(session)
        kwargs = _usage_write_kwargs(
            event_type, resolved_user_id, pair_id, member_id, source, meta,
        )
        kwargs['enforce_quota'] = enforce_quota
        if _can_use_external_connection(session):
            _release_clean_sqlite_read(session)
            return _log_usage_external(kwargs)
        return _log_usage_on_session(session, kwargs)
    except UsageQuotaError:
        _rollback_if_isolated(caller_open)
        raise
    except _UsageWriteSkipped:
        _rollback_if_isolated(caller_open)
        return None
    except Exception as exc:
        logger.debug("埋点写入失败，异常类型=%s", type(exc).__name__)
        _rollback_if_isolated(caller_open)
        return None
