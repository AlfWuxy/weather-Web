# -*- coding: utf-8 -*-
"""Audit helpers."""
import ipaddress
import json
import logging

from flask import current_app, g, request
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.orm import Session

from core.extensions import db
from core.db_models import AuditLog
from core.guest import is_guest_user
from core.security import hash_identifier

logger = logging.getLogger(__name__)

_AUDIT_SESSION_LISTENERS_REGISTERED = False
_AUDIT_BASELINE_ATTR = '_audit_sqlite_total_changes_base'
_AUDIT_HAS_FLUSHED_WRITES = '_audit_has_flushed_writes'
_AUDIT_HAS_DML = '_audit_has_dml'
_AUDIT_WRITE_FLAGS = (_AUDIT_HAS_FLUSHED_WRITES, _AUDIT_HAS_DML)
_AUDIT_ACTOR_ROLE_LEN = 20
_AUDIT_ACTION_LEN = 80
_AUDIT_RESOURCE_TYPE_LEN = 80
_AUDIT_RESOURCE_ID_LEN = 80
_AUDIT_IP_LEN = 64
_AUDIT_USER_AGENT_LEN = 200
_AUDIT_REQUEST_ID_LEN = 40


_TRUSTED_PROXY_CACHE_KEY = 'trusted_proxy_cidrs_cache'
_TRUSTED_PROXY_CACHE_RAW_KEY = 'trusted_proxy_cidrs_raw'


def _parse_ip(value):
    if not value:
        return None
    try:
        return ipaddress.ip_address(str(value).strip())
    except (ValueError, TypeError):
        return None


def _tokenize_cidrs(raw):
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = [str(item).strip() for item in raw if str(item).strip()]
    else:
        parts = [part.strip() for part in str(raw).replace(';', ',').split(',') if part.strip()]
    tokens = []
    for part in parts:
        tokens.extend([sub for sub in part.split() if sub])
    return tokens


def _trusted_proxy_networks():
    raw = current_app.config.get('TRUSTED_PROXY_CIDRS', '127.0.0.1/32,::1/128')
    cache_raw = current_app.extensions.get(_TRUSTED_PROXY_CACHE_RAW_KEY)
    cache_nets = current_app.extensions.get(_TRUSTED_PROXY_CACHE_KEY)
    if cache_raw == raw and isinstance(cache_nets, tuple):
        return cache_nets

    networks = []
    for token in _tokenize_cidrs(raw):
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("忽略无效 TRUSTED_PROXY_CIDRS 项: %s", token)

    result = tuple(networks)
    current_app.extensions[_TRUSTED_PROXY_CACHE_RAW_KEY] = raw
    current_app.extensions[_TRUSTED_PROXY_CACHE_KEY] = result
    return result


def _ip_in_trusted_networks(ip_obj, networks):
    return any(ip_obj in net for net in networks)


def _masked_ip_prefix(ip_obj):
    if ip_obj is None:
        return None
    if ip_obj.version == 4:
        octets = ip_obj.exploded.split('.')
        return f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
    hextets = ip_obj.exploded.split(':')
    return f"{hextets[0]}:{hextets[1]}:{hextets[2]}::/48"


def _get_client_ip_context():
    """提取客户端IP上下文：受信代理边界、来源、隐私化信息。"""
    remote_ip = _parse_ip(request.remote_addr)
    forwarded = request.headers.get('X-Forwarded-For', '')

    source = 'remote_addr'
    via_trusted_proxy = False
    client_ip = remote_ip

    if remote_ip and forwarded:
        networks = _trusted_proxy_networks()
        via_trusted_proxy = _ip_in_trusted_networks(remote_ip, networks)
        if via_trusted_proxy:
            forwarded_ips = []
            for token in forwarded.split(','):
                ip_obj = _parse_ip(token)
                if ip_obj is None:
                    logger.debug("X-Forwarded-For 中存在无效 IP，已忽略: %s", str(token)[:50])
                    continue
                forwarded_ips.append(ip_obj)
            if forwarded_ips:
                # 从右向左剥离受信代理，取最后一个非受信IP
                for candidate in reversed(forwarded_ips):
                    if not _ip_in_trusted_networks(candidate, networks):
                        client_ip = candidate
                        break
                else:
                    # 理论上很少出现：链路中均是受信代理，回退左端
                    client_ip = forwarded_ips[0]
                source = 'x_forwarded_for'

    client_ip_text = str(client_ip) if client_ip is not None else None
    return {
        'client_ip': client_ip_text,
        'ip_hash': hash_identifier(client_ip_text) if client_ip_text else None,
        'ip_prefix': _masked_ip_prefix(client_ip),
        'ip_source': source,
        'via_trusted_proxy': via_trusted_proxy,
    }


def _get_client_ip():
    """兼容旧调用：返回解析后的客户端 IP（不建议直接落库）。"""
    return _get_client_ip_context().get('client_ip')


def _sqlite_total_changes(sa_connection):
    """读取连接上的 SQLite total_changes；SELECT 本身不增加计数。"""
    fairy = sa_connection.connection
    raw = getattr(fairy, 'dbapi_connection', None)
    if raw is None:
        raw = getattr(fairy, 'driver_connection', None)
    total = getattr(raw, 'total_changes', None)
    if isinstance(total, int):
        return total
    return int(sa_connection.exec_driver_sql('SELECT total_changes()').scalar() or 0)


def _first_sa_connection(transaction):
    connections = getattr(transaction, '_connections', None) or {}
    seen = set()
    for item in connections.values():
        if not item:
            continue
        conn = item[0]
        if conn is None or id(conn) in seen:
            continue
        seen.add(id(conn))
        return conn
    return None


def _root_uses_sqlite(session, root):
    conn = _first_sa_connection(root)
    dialect = getattr(conn, 'dialect', None) if conn is not None else None
    if dialect is None:
        try:
            bind = session.get_bind()
        except Exception:
            return False
        dialect = getattr(bind, 'dialect', None)
    return getattr(dialect, 'name', None) == 'sqlite'


def _sqlite_dbapi_reports_transaction(root):
    """监听错过时的兜底：本驱动纯 SELECT 之后 in_transaction 为假。"""
    conn = _first_sa_connection(root)
    if conn is None:
        return False
    try:
        raw = conn.connection.dbapi_connection
    except Exception:
        return True
    if raw is None or not hasattr(raw, 'in_transaction'):
        return True
    return bool(raw.in_transaction)


def _root_transaction(session):
    getter = getattr(session, 'get_transaction', None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _mark_root_audit_flag(session, name):
    """把写标记打在根事务上。事务还没建出来时先寄存在 session.info。"""
    root = _root_transaction(session)
    if root is not None:
        setattr(root, name, True)
        return
    try:
        session.info[name] = True
    except Exception:
        return


def _audit_before_flush(session, flush_context, instances):
    try:
        _mark_root_audit_flag(session, _AUDIT_HAS_FLUSHED_WRITES)
    except Exception as exc:
        logger.debug('审计 flush 标记失败: %s', type(exc).__name__)


def _audit_do_orm_execute(orm_execute_state):
    try:
        if not (
            orm_execute_state.is_insert
            or orm_execute_state.is_update
            or orm_execute_state.is_delete
        ):
            return
        _mark_root_audit_flag(orm_execute_state.session, _AUDIT_HAS_DML)
    except Exception as exc:
        logger.debug('审计 DML 标记失败: %s', type(exc).__name__)


def _apply_pending_audit_flags(session, transaction):
    if transaction.parent is not None or transaction.nested:
        return
    try:
        info = session.info
    except Exception:
        return
    for name in _AUDIT_WRITE_FLAGS:
        if info.pop(name, False):
            setattr(transaction, name, True)


def _clear_pending_audit_flags(session):
    try:
        info = session.info
    except Exception:
        return
    for name in _AUDIT_WRITE_FLAGS:
        info.pop(name, None)


def _audit_after_transaction_create(session, transaction):
    if transaction.parent is not None or transaction.nested:
        return
    setattr(transaction, _AUDIT_BASELINE_ATTR, None)
    session.info[_AUDIT_BASELINE_ATTR] = None
    _apply_pending_audit_flags(session, transaction)


def _audit_after_begin(session, transaction, connection):
    # savepoint / 子事务不能覆盖根事务在启动时记下的基线。
    if transaction.parent is not None or transaction.nested:
        return
    if getattr(transaction, _AUDIT_BASELINE_ATTR, None) is not None:
        return
    if getattr(getattr(connection, 'dialect', None), 'name', None) != 'sqlite':
        return
    try:
        total = _sqlite_total_changes(connection)
    except Exception:
        try:
            delattr(transaction, _AUDIT_BASELINE_ATTR)
        except Exception:
            pass
        session.info.pop(_AUDIT_BASELINE_ATTR, None)
        return
    setattr(transaction, _AUDIT_BASELINE_ATTR, total)
    session.info[_AUDIT_BASELINE_ATTR] = total


def _drop_root_baseline_mirror(session):
    # get_transaction() 永远是根事务，区分不了 savepoint。
    if session.get_nested_transaction() is not None:
        return
    session.info.pop(_AUDIT_BASELINE_ATTR, None)


def _audit_after_commit(session):
    _drop_root_baseline_mirror(session)
    _clear_pending_audit_flags(session)


def _audit_after_rollback(session):
    _drop_root_baseline_mirror(session)
    _clear_pending_audit_flags(session)


def register_audit_session_listeners():
    """幂等注册 Session 事务监听，记录根事务启动时的 SQLite total_changes。"""
    global _AUDIT_SESSION_LISTENERS_REGISTERED
    if _AUDIT_SESSION_LISTENERS_REGISTERED:
        return
    _AUDIT_SESSION_LISTENERS_REGISTERED = True
    event.listen(Session, 'after_transaction_create', _audit_after_transaction_create)
    event.listen(Session, 'after_begin', _audit_after_begin)
    event.listen(Session, 'before_flush', _audit_before_flush)
    event.listen(Session, 'do_orm_execute', _audit_do_orm_execute)
    event.listen(Session, 'after_commit', _audit_after_commit)
    event.listen(Session, 'after_rollback', _audit_after_rollback)


def _root_sqlite_total_changes(root):
    conn = _first_sa_connection(root)
    if conn is None:
        return None
    try:
        return _sqlite_total_changes(conn)
    except Exception:
        return None


def _active_session(session):
    """scoped_session 没有代理事务状态方法，落到当前真正的 Session。"""
    if isinstance(session, Session):
        return session
    if getattr(session, 'session_factory', None) is not None and callable(session):
        return session()
    return session


def _marked_database_writes(session, root) -> bool:
    for name in _AUDIT_WRITE_FLAGS:
        if getattr(root, name, False):
            return True
        try:
            if session.info.get(name):
                return True
        except Exception:
            continue
    return False


def _sqlite_disk_writes(session, root) -> bool:
    """SQLite 根事务是否已经执行过会改变 total_changes 的语句。"""
    if not hasattr(root, _AUDIT_BASELINE_ATTR):
        return _sqlite_dbapi_reports_transaction(root)
    baseline = getattr(root, _AUDIT_BASELINE_ATTR)
    if baseline is None:
        return False
    current = _root_sqlite_total_changes(root)
    if current is None:
        return True
    return current != baseline


def _transaction_has_database_writes(session) -> bool:
    """根事务里是否已有数据库写入。内存里尚未 flush 的对象不算。"""
    session = _active_session(session)
    root = _root_transaction(session)
    if root is None:
        return False
    if not _root_uses_sqlite(session, root):
        return _marked_database_writes(session, root)
    if session.get_nested_transaction() is not None:
        return True
    return _sqlite_disk_writes(session, root)


def _has_uncommitted_writes(session) -> bool:
    """当前 Session 是否还有会随外层事务提交或回滚的写入。"""
    session = _active_session(session)
    if session.new or session.dirty or session.deleted:
        return True
    if getattr(session, '_flushing', False):
        return True

    root = _root_transaction(session)
    if root is None:
        return False
    if not _root_uses_sqlite(session, root):
        # 纯 SELECT 不会置位。PostgreSQL 的读事务因此可以走独立连接。
        return _marked_database_writes(session, root)
    if session.get_nested_transaction() is not None:
        return True
    return _sqlite_disk_writes(session, root)


def _log_audit_failure(exc):
    detail = type(exc).__name__
    try:
        from utils.log_safety import sanitize_log_text
        detail = sanitize_log_text(detail)
    except Exception:
        pass
    logger.warning('审计日志写入失败: %s', detail)


def _clip_optional(value, limit):
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _normalize_actor_id(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _normalize_action(value):
    if value is None:
        return ''
    text = value if isinstance(value, str) else str(value)
    return text.strip()[:_AUDIT_ACTION_LEN]


def _normalize_extra(value):
    if not isinstance(value, dict) or not value:
        return None
    try:
        return json.dumps(dict(value), ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _clean_audit_values(values):
    if not isinstance(values, dict):
        return None
    action = _normalize_action(values.get('action'))
    if not action:
        return None
    user_agent = _clip_optional(
        values.get('user_agent') if 'user_agent' in values else None,
        _AUDIT_USER_AGENT_LEN,
    )
    clean = {
        'actor_id': _normalize_actor_id(values.get('actor_id')),
        'actor_role': _clip_optional(values.get('actor_role'), _AUDIT_ACTOR_ROLE_LEN),
        'action': action,
        'resource_type': _clip_optional(values.get('resource_type'), _AUDIT_RESOURCE_TYPE_LEN),
        'resource_id': _clip_optional(values.get('resource_id'), _AUDIT_RESOURCE_ID_LEN),
        'extra_data': _normalize_extra(values.get('extra_data')),
        'ip_address': _clip_optional(values.get('ip_address'), _AUDIT_IP_LEN),
        # 缺失时与 log_audit 一样存空串，不写 NULL。
        'user_agent': '' if user_agent is None else user_agent,
        'request_id': _clip_optional(values.get('request_id'), _AUDIT_REQUEST_ID_LEN),
    }
    created_at = values.get('created_at') if 'created_at' in values else None
    if created_at is not None:
        clean['created_at'] = created_at
    return clean


def _execute_audit_while_flushing(session, stmt):
    """flush 进行中不能再开 Session 事务。非 SQLite 用连接级 savepoint 包住单条 INSERT。"""
    connection = session.connection()
    if getattr(getattr(connection, 'dialect', None), 'name', None) == 'sqlite':
        connection.execute(stmt)
        return
    with connection.begin_nested():
        connection.execute(stmt)


def _release_sqlite_read_lock(session):
    """只有 SQLite 的只读事务会占住文件锁。PostgreSQL 的 MVCC 读不必先 rollback。"""
    if not session.in_transaction():
        return
    root = _root_transaction(session)
    if root is not None and _root_uses_sqlite(session, root):
        session.rollback()
        return
    try:
        if db.engine.dialect.name == 'sqlite':
            session.rollback()
    except Exception:
        pass


def persist_audit_entry(values: dict) -> bool:
    """把审计行写入数据库。

    业务事务未提交时走 savepoint，和外层同生共死。
    纯读请求用独立连接提交，避免 teardown 回滚把审计行带走。
    PostgreSQL 读事务不阻塞这次提交；SQLite 要先释放文件锁。
    """
    try:
        clean = _clean_audit_values(values)
    except Exception as exc:
        _log_audit_failure(exc)
        return False
    if clean is None:
        return False

    try:
        session = _active_session(db.session)
        stmt = AuditLog.__table__.insert().values(**clean)
        if _has_uncommitted_writes(session):
            if session._flushing:
                _execute_audit_while_flushing(session, stmt)
            else:
                with session.begin_nested():
                    session.execute(stmt)
        else:
            _release_sqlite_read_lock(session)
            with db.engine.begin() as conn:
                conn.execute(stmt)
        return True
    except Exception as exc:
        _log_audit_failure(exc)
        return False


def log_audit(action, resource_type=None, resource_id=None, metadata=None):
    """记录审计日志（受Feature Flag控制）"""
    if not current_app.config.get('FEATURE_AUDIT_LOGS'):
        return None
    try:
        actor_id = None
        actor_role = None
        if current_user.is_authenticated:
            # 先收成纯标量。只读路径里的 rollback 会使已加载对象过期。
            actor_id = current_user.id if not is_guest_user(current_user) else None
            actor_role = getattr(current_user, 'role', None)
        ip_ctx = _get_client_ip_context()
        payload = metadata.copy() if isinstance(metadata, dict) else {}
        payload.setdefault('ip_source', ip_ctx.get('ip_source'))
        payload.setdefault('via_trusted_proxy', bool(ip_ctx.get('via_trusted_proxy')))
        if ip_ctx.get('ip_prefix'):
            payload.setdefault('ip_prefix', ip_ctx.get('ip_prefix'))
        values = {
            'actor_id': actor_id,
            'actor_role': actor_role,
            'action': action,
            'resource_type': resource_type,
            'resource_id': resource_id,
            'extra_data': payload,
            'ip_address': ip_ctx.get('ip_hash'),
            'user_agent': request.headers.get('User-Agent', ''),
            'request_id': getattr(g, 'request_id', None),
        }
    except Exception as exc:
        _log_audit_failure(exc)
        return None
    persist_audit_entry(values)
    return None
