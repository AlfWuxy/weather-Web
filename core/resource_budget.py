"""跨进程资源预算；数据库不可用时禁止继续付费调用。"""
import hashlib
import secrets
from datetime import timedelta

from flask import current_app, g, has_request_context
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


def acquire_lease(key, seconds=20):
    """固定散列槽限制锁表大小，同时合并跨 worker 的相同查询。"""
    slot = int(hashlib.sha256(key.encode()).hexdigest(), 16) % 256
    lock_key = f'geocode:{slot}'
    owner = secrets.token_hex(16)
    now = utcnow()
    table = ResourceLease.__table__
    try:
        with db.engine.begin() as conn:
            _insert_ignore(conn, table, {'key': lock_key, 'owner': '', 'expires_at': now - timedelta(seconds=1)})
            result = conn.execute(update(table).where(
                table.c.key == lock_key, table.c.expires_at < now,
            ).values(owner=owner, expires_at=now + timedelta(seconds=seconds)))
            return (lock_key, owner) if result.rowcount == 1 else None
    except SQLAlchemyError as exc:
        raise ResourceBudgetUnavailable('资源预算暂不可用') from exc


def release_lease(lease):
    table = ResourceLease.__table__
    try:
        with db.engine.begin() as conn:
            conn.execute(update(table).where(table.c.key == lease[0], table.c.owner == lease[1])
                         .values(expires_at=utcnow() - timedelta(seconds=1)))
    except SQLAlchemyError:
        pass
