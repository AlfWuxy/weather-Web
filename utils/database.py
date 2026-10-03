# -*- coding: utf-8 -*-
"""Database transaction helpers."""
from __future__ import annotations

from contextlib import contextmanager
import logging

from core.extensions import db

logger = logging.getLogger(__name__)


@contextmanager
def atomic_transaction(session=None):
    """Commit or roll back a transaction atomically.

    Usage:
        with atomic_transaction():
            db.session.add(model)
    """
    active_session = session or db.session
    try:
        yield active_session
        active_session.commit()
    except Exception as exc:
        active_session.rollback()
        # SQLAlchemy 异常正文和堆栈可能包含地址、凭证等绑定参数。
        logger.error("事务已回滚，异常类型=%s", type(exc).__name__)
        raise
