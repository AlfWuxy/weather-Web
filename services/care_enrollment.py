# -*- coding: utf-8 -*-
"""家人档案加入/退出天气照护。用户不必理解 Pair。"""
from __future__ import annotations

from datetime import timedelta

from core.db_models import FamilyMember, FamilyMemberProfile, Pair
from core.extensions import db
from core.time_utils import utcnow
from services.family_access import ensure_space_for_pair
from services.user._common import _create_pair_record
from utils.validators import sanitize_input


class CareEnrollmentError(Exception):
    def __init__(self, code, message, status_code=400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


def _profile_for(member):
    profile = FamilyMemberProfile.query.filter_by(member_id=member.id).first()
    if profile is None:
        profile = FamilyMemberProfile(member_id=member.id, alert_enabled=True)
        db.session.add(profile)
        db.session.flush()
    return profile


def find_active_pair_for_member(caregiver_id, member_id):
    if not caregiver_id or not member_id:
        return None
    return Pair.query.filter_by(
        caregiver_id=caregiver_id,
        member_id=member_id,
        status='active',
    ).first()


def find_recent_duplicate_member(user_id, name, relation, location_query, *, window_seconds=120):
    """重复提交保护：短窗口内同称呼+地点不另建活跃对象。不按历史姓名合并。"""
    if not user_id or not name:
        return None
    since = utcnow() - timedelta(seconds=window_seconds)
    candidates = (
        FamilyMember.query.filter_by(user_id=user_id, name=name)
        .filter(FamilyMember.created_at >= since)
        .order_by(FamilyMember.id.desc())
        .all()
    )
    location_query = (location_query or '').strip()
    relation = (relation or '').strip()
    for member in candidates:
        if (member.relation or '').strip() != relation:
            continue
        pair = find_active_pair_for_member(user_id, member.id)
        if pair and (pair.location_query or '').strip() == location_query:
            return member, pair
    return None


def enroll_weather_care(user, member, *, location_query, commit=False):
    """明确选择加入天气照护后，建立或复用 Pair。"""
    location_query = sanitize_input(location_query, max_length=200) or ''
    location_query = location_query.strip()
    if not location_query:
        raise CareEnrollmentError('missing_location', '加入天气照护前需要确认老人所在地，不能默认使用家属当前位置。')
    if member is None or getattr(member, 'id', None) is None:
        raise CareEnrollmentError('not_found', '家人档案不存在。', 404)
    if member.user_id != user.id and getattr(user, 'role', None) != 'admin':
        raise CareEnrollmentError('not_found', '家人档案不存在。', 404)

    profile = _profile_for(member)
    existing = find_active_pair_for_member(user.id, member.id)
    if existing:
        if location_query and existing.location_query != location_query:
            existing.location_query = location_query
            existing.community_code = location_query[:100]
        profile.location_query = location_query
        profile.weather_care_enabled = True
        ensure_space_for_pair(existing)
        db.session.flush()
        if commit:
            db.session.commit()
        return existing, False

    pair = _create_pair_record(
        caregiver_id=user.id,
        location_query=location_query,
        member_id=member.id,
        flush=True,
    )
    profile.location_query = location_query
    profile.weather_care_enabled = True
    ensure_space_for_pair(pair)
    db.session.flush()
    if commit:
        db.session.commit()
    return pair, True


def deactivate_weather_care(user, member, *, commit=False):
    """明确停用天气照护，不静默删除档案。"""
    if member is None:
        raise CareEnrollmentError('not_found', '家人档案不存在。', 404)
    if member.user_id != user.id and getattr(user, 'role', None) != 'admin':
        raise CareEnrollmentError('not_found', '家人档案不存在。', 404)
    profile = _profile_for(member)
    profile.weather_care_enabled = False
    pair = find_active_pair_for_member(user.id, member.id)
    if pair:
        pair.status = 'inactive'
    db.session.flush()
    if commit:
        db.session.commit()
    return pair
