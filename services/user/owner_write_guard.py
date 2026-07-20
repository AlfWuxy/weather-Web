# -*- coding: utf-8 -*-
"""Web 私密数据写入与账号注销共用的 owner 事务守卫。"""

from __future__ import annotations

from contextlib import contextmanager

from core.db_models import User
from core.extensions import db
from services.push.locks import push_owner_lock


class OwnerInactiveError(RuntimeError):
    """账号已注销或在取得写锁前失效。"""


def _normalize_owner_user_id(owner_user_id) -> int:
    try:
        normalized = int(owner_user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("owner_user_id 必须是正整数") from exc
    if normalized <= 0:
        raise ValueError("owner_user_id 必须是正整数")
    return normalized


def _lock_active_owner_for_write(owner_user_id: int):
    """取得数据库写锁，并在锁内重新读取未注销 owner。"""
    if db.engine.dialect.name == "sqlite":
        # SQLite 忽略 SELECT FOR UPDATE；条件 no-op UPDATE 会取得写锁并复核墓碑。
        lock_result = db.session.execute(
            db.update(User)
            .where(User.id == owner_user_id, User.deleted_at.is_(None))
            .values(last_login=User.last_login)
        )
        if lock_result.rowcount != 1:
            return None

    query = db.select(User).where(
        User.id == owner_user_id,
        User.deleted_at.is_(None),
    )
    if db.engine.dialect.name != "sqlite":
        query = query.with_for_update()
    return db.session.execute(
        query.execution_options(populate_existing=True)
    ).scalar_one_or_none()


@contextmanager
def owner_write_guard(owner_user_id):
    """按 file lock -> DB 的固定顺序保护一次 owner 私密写事务。

    调用方应先完成输入解析，再进入此守卫；并在守卫内重新查询资源归属、完成写入和提交。
    """
    normalized_owner_id = _normalize_owner_user_id(owner_user_id)
    # 丢弃认证与表单校验留下的陈旧读事务，避免与注销形成反向锁序。
    db.session.rollback()
    with push_owner_lock(normalized_owner_id):
        owner = _lock_active_owner_for_write(normalized_owner_id)
        if owner is None:
            db.session.rollback()
            raise OwnerInactiveError("owner account is inactive")
        try:
            yield owner
        except BaseException:
            db.session.rollback()
            raise
        else:
            # 正常调用必须已 commit；此处释放可能由提前 return 留下的读事务。
            db.session.rollback()
