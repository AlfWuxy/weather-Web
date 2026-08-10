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
        if not isinstance(user_id, str) or user_id.count(':') != 1:
            # 旧版纯数字 Cookie 不带撤销版本，升级后必须重新登录。
            return None
        raw_user_id, raw_auth_version = user_id.split(':', 1)
        if not (
            raw_user_id.isascii()
            and raw_user_id.isdecimal()
            and raw_auth_version.isascii()
            and raw_auth_version.isdecimal()
        ):
            return None
        try:
            # SQLAlchemy 2.x: 使用 session.get() 替代废弃的 query.get()
            from core.extensions import db
            normalized_user_id = int(raw_user_id)
            normalized_auth_version = int(raw_auth_version)
            if normalized_user_id <= 0 or normalized_auth_version <= 0:
                return None
            user = db.session.get(
                User,
                normalized_user_id,
                populate_existing=True,
            )
            if user is None or user.deleted_at is not None:
                return None
            if int(user.auth_version) != normalized_auth_version:
                return None
            return user
        except (TypeError, ValueError):
            return None
