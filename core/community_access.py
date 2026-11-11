# -*- coding: utf-8 -*-
"""患者派生数据统一使用管理员核准的社区范围。"""
import os

from flask_login import current_user


def patient_community_scope():
    """None 为管理员全域，非空元组为管辖范围，空元组为拒绝访问。"""
    if not current_user.is_authenticated:
        return ()
    if current_user.role == 'admin':
        return None
    if current_user.role == 'community':
        community = (getattr(current_user, 'authorized_community', None) or '').strip()
        if community:
            return (community,)
    return ()


def community_min_sample_size() -> int:
    """社区统计最小样本数。读取 COMMUNITY_MIN_SAMPLE_SIZE，默认 10，且不小于 1。"""
    raw = os.environ.get('COMMUNITY_MIN_SAMPLE_SIZE', '')
    if raw is None or str(raw).strip() == '':
        value = 10
    else:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = 10
    return max(value, 1)


def can_view_community_health_stats(community_name: str | None) -> bool:
    """管理员可看全部；社区账号只能看自己被授权的那一个社区。"""
    scope = patient_community_scope()
    if scope is None:
        return True
    name = (community_name or '').strip()
    if name and scope == (name,):
        return True
    return False
