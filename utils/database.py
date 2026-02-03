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
    except Exception:
        active_session.rollback()
        logger.exception("Transaction rolled back")
        raise
