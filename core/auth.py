# -*- coding: utf-8 -*-
"""Authentication hooks."""
from core.constants import GUEST_ID_PREFIX
from core.guest import build_guest_user
from core.db_models import User


def register_user_loader(login_manager):
    """Register the login manager user loader."""
    @login_manager.user_loader
    def load_user(user_id):
        if not user_id:
            return None
        if isinstance(user_id, str) and user_id.startswith(GUEST_ID_PREFIX):
            return build_guest_user(user_id)
        try:
            # SQLAlchemy 2.x: 使用 session.get() 替代废弃的 query.get()
            from core.extensions import db
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None
