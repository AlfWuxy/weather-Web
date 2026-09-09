# -*- coding: utf-8 -*-
"""User-facing routes."""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
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
    if not current_app.config.get('FEATURE_ELDER_MODE'):
        abort(404)
    return user_service.elder_dashboard()


@bp.route('/elder-mode/understood', methods=['POST'], endpoint='elder_mode_understood')
@login_required
def elder_mode_understood():
    if not current_app.config.get('FEATURE_ELDER_MODE'):
        abort(404)
    return user_service.handle_elder_mode_event('understood')


@bp.route('/elder-mode/select', methods=['POST'], endpoint='elder_mode_select')
@login_required
def elder_mode_select():
    if not current_app.config.get('FEATURE_ELDER_MODE'):
        abort(404)
    return user_service.handle_elder_mode_event('action_selected')


@bp.route('/elder-mode/confirm', methods=['POST'], endpoint='elder_mode_confirm')
@login_required
def elder_mode_confirm():
    if not current_app.config.get('FEATURE_ELDER_MODE'):
        abort(404)
    return user_service.handle_elder_mode_event('self_reported')


@bp.route('/elder-mode/help', methods=['POST'], endpoint='elder_mode_help')
@login_required
def elder_mode_help():
    if not current_app.config.get('FEATURE_ELDER_MODE'):
        abort(404)
    return user_service.handle_elder_mode_event('help_requested')


@bp.route('/elder-mode/state', methods=['GET'], endpoint='elder_mode_state')
@login_required
def elder_mode_state():
    if not current_app.config.get('FEATURE_ELDER_MODE'):
        abort(404)
    return user_service.handle_elder_mode_state()


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


@bp.route('/caregiver/help', endpoint='help_inbox')
@login_required
def help_inbox():
    """登录后的待处理求助工作台。"""
    from flask_login import current_user
    from services.help_request_service import list_help_requests
    data = list_help_requests(current_user, status='open', limit=50)
    return user_service.render_help_inbox(data)


@bp.route('/caregiver/help/<public_id>', endpoint='help_request_detail')
@login_required
def help_request_detail(public_id):
    from flask_login import current_user
    from services.help_request_service import get_help_request
    try:
        detail = get_help_request(current_user, public_id)
    except Exception:
        abort(404)
    return user_service.render_help_detail(detail)


@bp.route('/caregiver/invite', methods=['GET', 'POST'], endpoint='family_invite_accept')
@login_required
def family_invite_accept():
    """登录后预览并确认家庭邀请。GET 不消费。"""
    from flask_login import current_user
    from services.family_access import FamilyAccessError, consume_invite, preview_invite
    from core.extensions import db

    code = (request.values.get('code') or '').strip()
    preview = None
    error = None
    if code:
        try:
            preview = preview_invite(code)
        except FamilyAccessError as exc:
            error = exc.message
            preview = None
    if request.method == 'POST':
        if not code:
            flash('请填写邀请码。', 'warning')
        elif error:
            flash(error, 'warning')
        else:
            try:
                consume_invite(current_user, code)
                db.session.commit()
                flash('已加入家庭。现在可以看到同一照护对象。', 'success')
                return redirect(url_for('user.help_inbox'))
            except FamilyAccessError as exc:
                db.session.rollback()
                flash(exc.message, 'warning')
            except Exception:
                db.session.rollback()
                flash('加入家庭失败，请稍后重试。', 'danger')
    return render_template('family_invite.html', code=code, preview=preview, error=error)


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


@bp.route('/heat-exposure-gis', endpoint='heat_exposure_gis')
@login_required
def heat_exposure_gis():
    """都昌县 1 km 网格级热暴露 GIS"""
    if not current_app.config.get('FEATURE_HEAT_EXPOSURE_GIS'):
        abort(404)

    from services.heat_exposure_gis_service import render_heat_exposure_gis

    return render_heat_exposure_gis()


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
