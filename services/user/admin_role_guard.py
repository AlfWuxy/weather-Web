# -*- coding: utf-8 -*-
"""管理员角色变更的数据库级串行化保护。"""
from contextlib import contextmanager

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from core.db_models import User
from core.extensions import db


_POSTGRES_ADVISORY_LOCK_KEY = 383852783951


class LastAdminError(RuntimeError):
    """变更会移除最后一个管理员。"""


class AdminRoleGuardUnavailable(RuntimeError):
    """当前数据库无法提供管理员角色串行化锁。"""


class AdminRoleTargetMissing(LookupError):
    """加锁后目标用户已经不存在。"""


def _acquire_admin_role_lock():
    """在当前事务中获取跨进程数据库锁。"""
    bind = db.session.get_bind()
    dialect = bind.dialect.name
    try:
        if dialect == 'sqlite':
            # SQLite 的 FOR UPDATE 无效，必须先升级为写事务。
            db.session.execute(text('BEGIN IMMEDIATE'))
            return
        if dialect == 'postgresql':
            db.session.execute(
                text('SELECT pg_advisory_xact_lock(:lock_key)'),
                {'lock_key': _POSTGRES_ADVISORY_LOCK_KEY},
            )
            return
    except SQLAlchemyError as exc:
        raise AdminRoleGuardUnavailable('管理员角色锁获取失败') from exc
    raise AdminRoleGuardUnavailable(f'不支持的数据库方言: {dialect}')


@contextmanager
def serialized_admin_role_change(user_id, new_role):
    """锁定管理员角色集合，重新读取目标并检查最后管理员约束。"""
    # 表单校验查询会自动开启读事务，先释放快照再获取写锁。
    db.session.rollback()
    try:
        _acquire_admin_role_lock()
        target = db.session.get(User, int(user_id), populate_existing=True)
        if target is None:
            raise AdminRoleTargetMissing('目标用户不存在')

        if target.role == 'admin' and new_role != 'admin':
            admin_count = db.session.scalar(
                select(func.count(User.id)).where(User.role == 'admin')
            )
            if int(admin_count or 0) <= 1:
                raise LastAdminError('必须保留至少一个管理员账户')

        yield target
    except Exception:
        db.session.rollback()
        raise
