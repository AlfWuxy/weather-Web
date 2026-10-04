# -*- coding: utf-8 -*-
"""游客个人与家庭照护体验，所有写入仅进入隔离的临时缓存。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, g, make_response, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from core.constants import CHRONIC_OPTIONS, GUEST_ID_PREFIX
from core.extensions import limiter
from core.guest import is_guest_user
from core.security import rate_limit_key
from services.guest_experience import GuestExperienceError, get_experience, mutate_experience


bp = Blueprint('guest_experience', __name__, url_prefix='/experience')
VIEWS = frozenset({'overview', 'profile', 'family', 'checkin', 'diary', 'medications'})
ACTION_OPTIONS = [('drink_water', '补水'), ('rest', '休息'), ('contact', '联系家人')]
OPERATION_FIELDS = {
    'profile': {'username', 'age', 'gender', 'community', 'has_chronic_disease', 'chronic_diseases'},
    'member_save': {'id', 'name', 'age', 'relationship'},
    'member_delete': {'id'},
    'checkin': {'member_id', 'actions'},
    'review': {'member_id'},
    'diary_save': {'id', 'member_id', 'date', 'symptoms', 'severity', 'notes'},
    'diary_delete': {'id'},
    'medication_save': {'id', 'member_id', 'name', 'time', 'note'},
    'medication_delete': {'id'},
    'reset': set(),
}


def _form_payload():
    operation = request.form.get('operation')
    if operation not in OPERATION_FIELDS:
        raise GuestExperienceError('暂不支持这项体验操作。')
    fields = OPERATION_FIELDS[operation]
    if set(request.form) - fields - {'csrf_token', 'version', 'operation'}:
        raise GuestExperienceError('提交内容格式不正确，请刷新后重试。')
    payload = {field: request.form[field] for field in fields if field in request.form}
    try:
        version = int(request.form.get('version', ''))
        if 'age' in payload:
            payload['age'] = int(payload['age'])
    except (TypeError, ValueError):
        raise GuestExperienceError('年龄或页面版本无效，请检查后重试。') from None
    if operation == 'profile':
        raw_chronic = request.form.get('has_chronic_disease', '')
        if raw_chronic not in {'', '0', '1', 'on', 'true', 'false'}:
            raise GuestExperienceError('请选择是否存在慢性病。')
        payload['has_chronic_disease'] = raw_chronic in {'1', 'on', 'true'}
        payload['chronic_diseases'] = request.form.getlist('chronic_diseases')
    if operation == 'checkin':
        payload['actions'] = request.form.getlist('actions')
    return operation, payload, version


def _unavailable(error):
    response = make_response(render_template('guest_experience_unavailable.html', error=error), error.status_code)
    response.headers['Cache-Control'] = 'no-store, private'
    return response


@bp.app_errorhandler(GuestExperienceError)
def handle_store_failure(error):
    # 资料已载入后仍可能遇到缓存中断，所有读取路径统一返回明确失败。
    return _unavailable(error)


@bp.before_app_request
def require_available_guest_profile():
    # 身份加载失败时停止后续计算，避免使用默认年龄产生与游客资料不符的结果。
    g.guest_experience_identity = (
        request.endpoint != 'static'
        and str(session.get('_user_id', '')).startswith(GUEST_ID_PREFIX)
    )
    if request.endpoint in {'static', 'public.logout', 'public.login', 'public.register', 'public.guest_login'}:
        return None
    # 机构工作台先使用既有角色门禁，过期游客仍应得到明确的无权限结果。
    if request.blueprint == 'workbench':
        return None
    if g.guest_experience_identity:
        error = getattr(current_user, 'experience_error', None)
        if error is not None:
            return _unavailable(error)
    return None


@bp.after_app_request
def prevent_guest_caching(response):
    # 不在响应阶段读取匿名 session，保留首页原有的 Cloudflare 微缓存边界。
    if request.blueprint == bp.name or request.endpoint == 'public.guest_login' or getattr(g, 'guest_experience_identity', False):
        response.headers['Cache-Control'] = 'no-store, private'
        response.vary.add('Cookie')
    return response


@bp.route('/', defaults={'view': 'overview'}, methods=['GET', 'POST'])
@bp.route('/<view>', methods=['GET', 'POST'])
@login_required
@limiter.limit('90 per minute', methods=['GET'], key_func=rate_limit_key)
@limiter.limit('30 per 10 minutes', methods=['POST'], key_func=rate_limit_key)
@limiter.limit('10 per hour', methods=['POST'], key_func=rate_limit_key,
               exempt_when=lambda: request.form.get('operation') != 'reset')
def page(view):
    if not is_guest_user(current_user):
        abort(403)
    if view not in VIEWS:
        abort(404)
    if request.content_length is not None and request.content_length > 16 * 1024:
        abort(413)
    error_message = None
    status = 200
    try:
        if request.method == 'POST':
            operation, payload, version = _form_payload()
            # 身份始终来自服务器验证过的登录态，不接受表单中的游客标识。
            mutate_experience(current_user.id, operation, payload, version)
            flash('体验记录已更新，仅临时保留；不会发送真实提醒。', 'success')
            return redirect(url_for('guest_experience.page', view=view), code=303)
        state = get_experience(current_user.id, create=False)
        if state is None:
            raise GuestExperienceError('这次游客体验已结束，请重新进入体验。', 409)
    except GuestExperienceError as error:
        status = error.status_code
        error_message = str(error)
        try:
            state = get_experience(current_user.id, create=False)
        except GuestExperienceError:
            return _unavailable(error)
        if state is None:
            return _unavailable(error)
    return render_template(
        'guest_experience.html', state=state, view=view, error=error_message,
        action_options=ACTION_OPTIONS, chronic_options=CHRONIC_OPTIONS,
        expires_label=datetime.fromtimestamp(state['expires_at'], ZoneInfo('Asia/Shanghai')).strftime('%H:%M'),
    ), status
