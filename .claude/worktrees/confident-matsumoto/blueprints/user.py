# -*- coding: utf-8 -*-
"""User-facing routes."""
from flask import Blueprint, current_app
from flask_login import login_required

from core.extensions import limiter
from core.security import rate_limit_key
from services import user_service

bp = Blueprint('user', __name__)


@bp.route('/dashboard', endpoint='user_dashboard')
@login_required
def user_dashboard():
    """用户仪表板"""
    return user_service.user_dashboard()


@bp.route('/elder-mode', endpoint='elder_dashboard')
@login_required
def elder_dashboard():
    """极简老人模式入口"""
    return user_service.elder_dashboard()


@bp.route('/pairs', methods=['GET', 'POST'], endpoint='pair_management')
@login_required
def pair_management():
    """照护绑定管理"""
    return user_service.pair_management()


@bp.route('/caregiver', endpoint='caregiver_dashboard')
@login_required
def caregiver_dashboard():
    """照护人工作台"""
    return user_service.caregiver_dashboard()


@bp.route('/caregiver/pair/create', methods=['POST'], endpoint='caregiver_pair_create')
@login_required
def caregiver_pair_create():
    """照护人创建绑定短码"""
    return user_service.caregiver_pair_create()


@bp.route('/caregiver/pair/<int:pair_id>', endpoint='caregiver_pair_detail')
@login_required
def caregiver_pair_detail(pair_id):
    """照护关系详情"""
    return user_service.caregiver_pair_detail(pair_id)


@bp.route('/caregiver/pair/<int:pair_id>/action-log', methods=['POST'], endpoint='caregiver_action_log')
@login_required
def caregiver_action_log(pair_id):
    """照护行动记录"""
    return user_service.caregiver_action_log(pair_id)


@bp.route('/caregiver/wechat_template', endpoint='caregiver_wechat_template')
@login_required
def caregiver_wechat_template():
    """照护人微信模板"""
    return user_service.caregiver_wechat_template()


@bp.route('/pairs/<int:pair_id>/escalate', methods=['POST'], endpoint='pair_escalate')
@login_required
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_ESCALATE', '10 per hour'), key_func=rate_limit_key)
def pair_escalate(pair_id):
    """升级链推进"""
    return user_service.pair_escalate(pair_id)


@bp.route('/pairs/<int:pair_id>/backup', methods=['POST'], endpoint='pair_backup_contact')
@login_required
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_ESCALATE', '10 per hour'), key_func=rate_limit_key)
def pair_backup_contact(pair_id):
    """标记已联系备选联系人"""
    return user_service.pair_backup_contact(pair_id)


@bp.route('/caregiver/relay/escalate', methods=['POST'], endpoint='caregiver_relay_escalate')
@login_required
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_ESCALATE', '10 per hour'), key_func=rate_limit_key)
def caregiver_relay_escalate():
    """照护人升级链推进"""
    return user_service.caregiver_relay_escalate()


@bp.route('/caregiver/relay/backup', methods=['POST'], endpoint='caregiver_relay_backup')
@login_required
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_ESCALATE', '10 per hour'), key_func=rate_limit_key)
def caregiver_relay_backup():
    """照护人标记备选联系人已联系"""
    return user_service.caregiver_relay_backup()


@bp.route('/community', endpoint='community_dashboard')
@login_required
def community_dashboard():
    """社区工作台"""
    return user_service.community_dashboard()


@bp.route('/community/<community_code>', endpoint='community_detail')
@login_required
def community_detail(community_code):
    """社区详情"""
    return user_service.community_detail(community_code)


@bp.route('/community/<community_code>/wechat', endpoint='community_wechat')
@login_required
def community_wechat(community_code):
    """社区微信模板"""
    return user_service.community_wechat(community_code)


@bp.route('/community/announce', endpoint='community_announce')
@login_required
def community_announce():
    """公共传播包生成器"""
    return user_service.community_announce()


@bp.route('/health-assessment', methods=['GET', 'POST'], endpoint='health_assessment')
@login_required
def health_assessment():
    """健康风险评估"""
    return user_service.health_assessment()


@bp.route('/community-risk', endpoint='community_risk')
@login_required
def community_risk():
    """社区风险地图"""
    return user_service.community_risk()


@bp.route('/profile', methods=['GET', 'POST'], endpoint='profile')
@login_required
def profile():
    """个人设置"""
    return user_service.profile()


@bp.route('/location', methods=['POST'], endpoint='update_location')
@login_required
def update_location():
    """更新当前位置"""
    return user_service.update_location()
