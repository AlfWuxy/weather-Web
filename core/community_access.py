# -*- coding: utf-8 -*-
"""患者派生数据只采用管理员核准的社区范围。"""
from flask_login import current_user


def patient_community_scope():
    """None 为管理员全域，元组为管辖范围，空元组为拒绝访问。"""
    if not current_user.is_authenticated or getattr(current_user, 'deleted_at', None):
        return ()
    if current_user.role == 'admin':
        return None
    if current_user.role == 'community':
        community = (getattr(current_user, 'authorized_community', None) or '').strip()
        if community:
            return (community,)
    return ()
