# -*- coding: utf-8 -*-
"""Guest user helpers."""
from datetime import datetime
import logging
from types import SimpleNamespace
import secrets

from flask import session
from flask_login import UserMixin

from core.constants import GUEST_ID_PREFIX
from core.time_utils import utcnow

logger = logging.getLogger(__name__)


class GuestUser(UserMixin):
    """游客用户（不入库）"""
    def __init__(self, guest_id, profile):
        self.id = guest_id
        self.username = profile.get('username', '游客')
        self.email = None
        self.role = 'guest'
        self.age = profile.get('age')
        self.gender = profile.get('gender', '未知')
        self.community = profile.get('community', '朝阳社区')
        self.has_chronic_disease = profile.get('has_chronic_disease', False)
        self.chronic_diseases = profile.get('chronic_diseases')
        self.is_guest = True


def is_guest_user(user):
    return bool(getattr(user, 'is_guest', False))


def build_guest_profile():
    profile = session.get('guest_profile')
    if not profile:
        profile = {
            'username': '游客',
            'age': None,
            'gender': '未知',
            'community': '朝阳社区',
            'has_chronic_disease': False,
            'chronic_diseases': None
        }
        session['guest_profile'] = profile
    return profile


def build_guest_user(guest_id=None):
    profile = build_guest_profile()
    if not guest_id:
        guest_id = session.get('guest_id')
    if not guest_id:
        guest_id = f"{GUEST_ID_PREFIX}{secrets.token_urlsafe(12)}"
        session['guest_id'] = guest_id
    return GuestUser(guest_id, profile)


def get_guest_assessment():
    data = session.get('guest_assessment')
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
