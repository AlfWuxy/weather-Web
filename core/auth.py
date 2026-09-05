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
            # 正式账号只接受 get_id() 生成的 "{id}:{password_stamp}" 格式。
            raw = str(user_id)
            uid_part, separator, stamp_part = raw.partition(':')
            if (
                separator != ':'
                or ':' in stamp_part
                or not uid_part.isascii()
                or not uid_part.isdigit()
            ):
                return None
            user = db.session.get(User, int(uid_part))
            if not user:
                return None
            import hashlib
            expected = hashlib.sha256((user.password_hash or '').encode('utf-8')).hexdigest()[:16]
            if raw != f'{user.id}:{expected}':
                # 拒绝旧纯数字会话、畸形 ID 与改密前的密码戳。
                return None
            return user
        except (OverflowError, TypeError, ValueError):
            return None
