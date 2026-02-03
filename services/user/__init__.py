# -*- coding: utf-8 -*-
"""User services public API."""

from .dashboard_service import elder_dashboard, user_dashboard
from .caregiver_service import (
    caregiver_action_log,
    caregiver_dashboard,
    caregiver_pair_create,
    caregiver_pair_detail,
    caregiver_relay_backup,
    caregiver_relay_escalate,
    caregiver_wechat_template,
    pair_backup_contact,
    pair_escalate,
    pair_management
)
from .community_service import (
    community_announce,
    community_dashboard,
    community_detail,
    community_risk,
    community_wechat
)
from .profile_service import health_assessment, profile, update_location

__all__ = [
    'user_dashboard',
    'elder_dashboard',
    'pair_management',
    'caregiver_dashboard',
    'caregiver_pair_create',
    'caregiver_pair_detail',
    'caregiver_action_log',
    'caregiver_wechat_template',
    'pair_escalate',
    'pair_backup_contact',
    'caregiver_relay_escalate',
    'caregiver_relay_backup',
    'community_dashboard',
    'community_detail',
    'community_wechat',
    'community_announce',
    'community_risk',
    'health_assessment',
    'profile',
    'update_location'
]
