# -*- coding: utf-8 -*-
"""Authentication hooks."""
import hmac

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
        raw = str(user_id or '')
        uid_part, sep, stamp_part = raw.partition(':')
        # 只接受 ASCII 数字 id 与 16 位小写十六进制戳。
        # '²'.isdigit() 为真但不能 int()；非 [0-9a-f] 不能进 compare_digest。
        stamp_is_hex = len(stamp_part) == 16 and all(
            '0' <= char <= '9' or 'a' <= char <= 'f' for char in stamp_part
        )
        if (
            not sep
            or not uid_part.isascii()
            or not uid_part.isdigit()
            or not 1 <= len(uid_part) <= 18
            or not stamp_is_hex
        ):
            return None
        # SQLAlchemy 2.x: 使用 session.get() 替代废弃的 query.get()
        from core.extensions import db
        try:
            user = db.session.get(User, int(uid_part))
            if user is None or not user.password_hash:
                return None
            if not hmac.compare_digest(user.password_session_stamp(), stamp_part):
                return None
        except (ValueError, TypeError, OverflowError):
            return None
        return user
