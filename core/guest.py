# -*- coding: utf-8 -*-
"""游客身份与临时体验资料。"""
from datetime import datetime
import json
import logging
from types import SimpleNamespace
import secrets

from flask import Response, session
from flask_login import UserMixin, current_user

from core.constants import DEFAULT_CITY_LABEL, GUEST_ID_PREFIX
from core.time_utils import utcnow
from services.guest_experience import (
    GuestExperienceError,
    get_experience,
    mutate_experience,
)

logger = logging.getLogger(__name__)


class GuestUser(UserMixin):
    """游客用户（不入库）"""
    def __init__(self, guest_id, profile):
        self.id = guest_id
        self.email = None
        self.role = 'guest'
        self.is_guest = True
        self.experience_error = None
        self.update_profile(profile)

    def update_profile(self, profile):
        """与正式用户保持相同的只读字段接口，不建立数据库用户。"""
        self.username = profile.get('username', '示例长者')
        self.age = profile.get('age', 65)
        self.gender = profile.get('gender', '未知')
        self.community = profile.get('community', DEFAULT_CITY_LABEL)
        self.has_chronic_disease = profile.get('has_chronic_disease', False)
        self.chronic_diseases = json.dumps(
            profile.get('chronic_diseases') or [], ensure_ascii=False
        )


def is_guest_user(user):
    return bool(getattr(user, 'is_guest', False))


def _resolve_guest_id(guest_id=None):
    """Cookie 只保留不透明身份，不保存健康资料或评估内容。"""
    if not guest_id:
        guest_id = session.get('guest_id')
    if not guest_id:
        session_user_id = session.get('_user_id')
        if isinstance(session_user_id, str) and session_user_id.startswith(GUEST_ID_PREFIX):
            guest_id = session_user_id
    if not guest_id:
        guest_id = f"{GUEST_ID_PREFIX}{secrets.token_urlsafe(12)}"
    session['guest_id'] = guest_id
    session.pop('guest_profile', None)
    session.pop('guest_assessment', None)
    return guest_id


def build_guest_profile(guest_id=None):
    return _existing_experience(_resolve_guest_id(guest_id))['profile']


def _existing_experience(guest_id):
    state = get_experience(guest_id, create=False)
    if state is None:
        raise GuestExperienceError('这次游客体验已结束，请重新进入体验。', 409)
    return state


def update_guest_profile(partial):
    """显式更新临时资料；版本冲突交由调用方提示，避免覆盖其他标签页。"""
    guest_id = _resolve_guest_id()
    state = _existing_experience(guest_id)
    profile = dict(state['profile'])
    profile.update(partial)
    updated = mutate_experience(guest_id, 'profile', profile, state['version'])
    if is_guest_user(current_user) and current_user.id == guest_id:
        current_user.update_profile(updated['profile'])
        current_user.experience_version = updated['version']
    return updated['profile']


def build_guest_user(guest_id=None):
    guest_id = _resolve_guest_id(guest_id)
    try:
        state = _existing_experience(guest_id)
        user = GuestUser(guest_id, state['profile'])
        user.experience_version = state['version']
        return user
    except GuestExperienceError as exc:
        # 身份加载不能递归触发错误页；依赖临时资料的请求由入口门禁拒绝。
        user = GuestUser(guest_id, {})
        user.experience_error = exc
        return user


def guest_experience_error_response(error):
    """存储异常时明确告知未保存，避免渲染模板再次加载游客身份。"""
    response = Response(str(error), status=error.status_code, mimetype='text/plain')
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def get_guest_assessment():
    data = _existing_experience(_resolve_guest_id()).get('assessment')
    if not data:
        return None
    raw_date = data.get('assessment_date')
    try:
        assessment_date = datetime.fromisoformat(raw_date) if raw_date else None
        if assessment_date is None:
            raise ValueError("missing assessment_date")
    except (TypeError, ValueError) as exc:
        logger.warning("Guest assessment date parse failed: %s", exc)
        assessment_date = utcnow()
    return SimpleNamespace(
        assessment_date=assessment_date,
        risk_level=data.get('risk_level'),
        risk_score=data.get('risk_score'),
        recommendations=data.get('recommendations'),
        explain=data.get('explain')
    )
