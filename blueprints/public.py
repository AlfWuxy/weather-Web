# -*- coding: utf-8 -*-
"""Public and auth routes."""
from flask import Blueprint, current_app, redirect, render_template, request, url_for
from flask_login import login_required

from core.extensions import limiter
from core.security import rate_limit_key
from core.time_utils import today_local
from services.public_service import (
    render_role_entry,
    handle_login,
    handle_register,
    render_cooling_resources_page,
    render_public_risk_page,
    handle_guest_login,
    handle_logout,
    _handle_action_lookup,
    _handle_action_confirm,
    _handle_action_help,
    _handle_action_debrief,
    _resolve_pair_from_session_or_code,
    _build_action_context,
    _resolve_action_routes,
    _render_action_page
)
from utils.validators import sanitize_input

bp = Blueprint('public', __name__)


@bp.route('/', endpoint='index')
def index():
    """首页"""
    return render_template('index.html')


@bp.route('/entry', endpoint='role_entry')
def role_entry():
    """角色选择入口"""
    return render_role_entry()


@bp.route('/login', methods=['GET', 'POST'], endpoint='login')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_LOGIN', '5 per 5 minutes'), methods=['POST'], key_func=rate_limit_key)
def login():
    """登录"""
    next_url = sanitize_input(request.args.get('next') or request.form.get('next'), max_length=200)
    return handle_login(next_url)


@bp.route('/register', methods=['GET', 'POST'], endpoint='register')
def register():
    """注册"""
    return handle_register()


@bp.route('/action', methods=['GET', 'POST'], endpoint='action_check')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_SHORT_CODE', '3 per hour'), methods=['POST'], key_func=rate_limit_key)
def action_check():
    """短码行动确认入口"""
    token = sanitize_input(request.args.get('token'), max_length=200)
    return _handle_action_lookup(token=token)

@bp.route('/action/confirm', methods=['POST'], endpoint='action_confirm')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_CONFIRM', '30 per hour'), key_func=rate_limit_key)
def action_confirm():
    """行动确认"""
    return _handle_action_confirm()


@bp.route('/action/help', methods=['POST'], endpoint='action_help')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_HELP', '10 per hour'), key_func=rate_limit_key)
def action_help():
    """发出求助"""
    return _handle_action_help()


@bp.route('/action/debrief', methods=['POST'], endpoint='action_debrief')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_CONFIRM', '30 per hour'), key_func=rate_limit_key)
def action_debrief():
    """行动复盘"""
    return _handle_action_debrief()


@bp.route('/elder', methods=['GET'], endpoint='elder_entry')
def elder_entry():
    """长者行动入口（短码）"""
    token = sanitize_input(request.args.get('token'), max_length=200)
    return _handle_action_lookup(token=token, entry_action=url_for('public.elder_enter'))


@bp.route('/elder/enter', methods=['POST'], endpoint='elder_enter')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_SHORT_CODE', '3 per hour'), key_func=rate_limit_key)
def elder_enter():
    """长者短码确认"""
    return _handle_action_lookup(entry_action=url_for('public.elder_enter'))


@bp.route('/e/<token>', methods=['GET'], endpoint='elder_token_entry')
def elder_token_entry(token):
    """带令牌的绑定入口"""
    token = sanitize_input(token, max_length=200)
    return _handle_action_lookup(token=token, entry_action=url_for('public.elder_enter'))


@bp.route('/e/<token>/checkin', methods=['POST'], endpoint='elder_token_checkin')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_CONFIRM', '30 per hour'), key_func=rate_limit_key)
def elder_token_checkin(token):
    """带令牌确认"""
    token = sanitize_input(token, max_length=200)
    return _handle_action_confirm(token=token)


@bp.route('/e/<token>/help', methods=['POST'], endpoint='elder_token_help')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_HELP', '10 per hour'), key_func=rate_limit_key)
def elder_token_help(token):
    """带令牌求助"""
    token = sanitize_input(token, max_length=200)
    return _handle_action_help(token=token)


@bp.route('/e/<token>/debrief', methods=['GET', 'POST'], endpoint='elder_token_debrief')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_CONFIRM', '30 per hour'), methods=['POST'], key_func=rate_limit_key)
def elder_token_debrief(token):
    """带令牌复盘"""
    token = sanitize_input(token, max_length=200)
    if request.method == 'POST':
        return _handle_action_debrief(token=token, focus_debrief=True)

    short_code = sanitize_input(request.args.get('short_code'), max_length=12)
    pair = _resolve_pair_from_session_or_code(short_code)
    if not pair:
        return redirect(url_for('public.elder_token_entry', token=token))

    status_date = today_local()
    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, status_date
    )
    action_routes = _resolve_action_routes()
    return _render_action_page(
        pair,
        status,
        actions,
        resources,
        weather_data,
        heat_result,
        risk_label,
        risk_reasons=risk_reasons,
        focus_debrief=True,
        **action_routes
    )


@bp.route('/transparency', endpoint='transparency')
def transparency():
    """透明度说明"""
    return render_template('transparency.html')


@bp.route('/cooling', endpoint='cooling_resources')
def cooling_resources():
    """避暑资源公开页"""
    community = sanitize_input(request.args.get('community'), max_length=100)
    resource_type = sanitize_input(request.args.get('resource_type'), max_length=50)
    has_ac_raw = request.args.get('has_ac')
    is_accessible_raw = request.args.get('is_accessible')
    open_only = request.args.get('open_only')
    return render_cooling_resources_page(
        community=community,
        resource_type=resource_type,
        has_ac_raw=has_ac_raw,
        is_accessible_raw=is_accessible_raw,
        open_only=open_only
    )


@bp.route('/risk', endpoint='public_risk')
def public_risk():
    """公开风险与行动建议"""
    location = sanitize_input(request.args.get('location'), max_length=100)
    return render_public_risk_page(location)


@bp.route('/guest', endpoint='guest_login')
def guest_login():
    """游客模式入口"""
    return handle_guest_login()


@bp.route('/logout', endpoint='logout')
@login_required
def logout():
    """登出"""
    return handle_logout()
