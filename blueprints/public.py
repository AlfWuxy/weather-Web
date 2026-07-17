# -*- coding: utf-8 -*-
"""Public and auth routes."""
import logging
from urllib.parse import parse_qsl

import requests
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

from core.extensions import limiter
from core.extensions import db
from core.security import rate_limit_key
from core.time_utils import utcnow
from core.usage import log_usage_event
from core.db_models import AlertDelivery
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
    _validate_pair_token_binding,
    _build_action_context,
    _resolve_action_routes,
    _render_action_page
)
from utils.validators import sanitize_input

bp = Blueprint('public', __name__)
ALLOWED_AMAP_PROXY_PATHS = {'v3/place/text'}
AMAP_PROXY_MAX_BYTES = 256 * 1024
ROBOTS_TXT = """User-agent: *
Allow: /
Disallow: /admin
Disallow: /api/
Disallow: /mp/api/
Disallow: /dashboard
Disallow: /caregiver
Disallow: /community
Disallow: /community-risk
Disallow: /logout
Disallow: /profile
Disallow: /family-members
Disallow: /pairs
Disallow: /location
Disallow: /health-assessment
Disallow: /medication-reminders
Disallow: /health-diary
Disallow: /forecast-7day
Disallow: /ml-prediction
Disallow: /ai-qa
Disallow: /chronic-risk
Disallow: /annual-report
Disallow: /analysis/
Disallow: /alerts/
Disallow: /reports
Disallow: /guest
Disallow: /action
Disallow: /elder
Disallow: /e/
Disallow: /t/
"""

HOME_EDGE_CACHE_SECONDS = 60
HOME_STALE_WHILE_REVALIDATE_SECONDS = 30


def _is_cacheable_anonymous_home():
    """仅允许无登录态、无认证 Cookie、无查询参数的首页进入边缘缓存。"""
    session_cookie_name = current_app.config.get('SESSION_COOKIE_NAME', 'session')
    remember_cookie_name = current_app.config.get('REMEMBER_COOKIE_NAME', 'remember_token')
    private_cookie_names = {session_cookie_name, remember_cookie_name} - {None, ''}
    has_private_cookie = any(name in request.cookies for name in private_cookie_names)
    return (
        request.method in {'GET', 'HEAD'}
        and not request.query_string
        and not has_private_cookie
        and not current_user.is_authenticated
    )


@bp.route('/robots.txt', endpoint='robots_txt')
def robots_txt():
    """允许搜索与 AI 爬虫抓取公开页面。"""
    return Response(ROBOTS_TXT, content_type='text/plain; charset=utf-8')


@bp.route('/healthz', endpoint='healthz')
@limiter.exempt
def healthz():
    """仅检查应用与数据库，不读取天气或其他外部服务。"""
    try:
        db.session.execute(text('SELECT 1')).scalar_one()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('健康检查数据库查询失败')
        response = jsonify({'status': 'unavailable'})
        response.status_code = 503
    else:
        response = jsonify({'status': 'ok'})
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/', endpoint='index')
def index():
    """首页"""
    cacheable_anonymous = _is_cacheable_anonymous_home()
    template_context = {}
    if cacheable_anonymous:
        # 匿名首页没有写操作，避免生成 CSRF Token 时创建 Session Cookie。
        template_context['csrf_token'] = lambda: ''

    response = make_response(render_template('index.html', **template_context))
    if cacheable_anonymous and not session.modified:
        # 浏览器不落盘，Cloudflare 边缘短缓存并在后台刷新。
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Cloudflare-CDN-Cache-Control'] = (
            f'public, max-age={HOME_EDGE_CACHE_SECONDS}, '
            f'stale-while-revalidate={HOME_STALE_WHILE_REVALIDATE_SECONDS}'
        )
        # 已确认请求没有会话 Cookie，清除只读 Session 访问产生的 Vary: Cookie。
        session.accessed = False
    else:
        # 登录态、已有会话或带查询参数的首页必须绕过所有共享缓存。
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Cloudflare-CDN-Cache-Control'] = 'no-store'
    return response


@bp.route('/entry', endpoint='role_entry')
def role_entry():
    """角色选择入口"""
    return render_role_entry()


@bp.route('/login', methods=['GET', 'POST'], endpoint='login')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_LOGIN', '5 per 5 minutes'), methods=['POST'], key_func=rate_limit_key)
def login():
    """登录"""
    # URL 不经过 HTML 清理，避免把 & 重复转义为 &amp;。
    raw_next = request.args.get('next') or request.form.get('next')
    next_url = str(raw_next)[:200] if raw_next else None
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
    if not pair or not _validate_pair_token_binding(pair, short_code, token):
        return redirect(url_for('public.elder_token_entry', token=token))

    status_date = today_local()
    status, actions, resources, weather_data, heat_result, risk_label, risk_reasons = _build_action_context(
        pair, status_date
    )
    action_routes = _resolve_action_routes(token=token)
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
    # 兼容旧版 location 参数，同时统一交给后端查询，避免地图数据仍混入其他社区。
    community = sanitize_input(
        request.args.get('community') or request.args.get('location'),
        max_length=100,
    )
    resource_type = sanitize_input(
        request.args.get('resource_type') or request.args.get('type'),
        max_length=50
    )
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


@bp.route('/_AMapService/<path:proxy_path>')
@limiter.limit(lambda: current_app.config.get('RATE_LIMIT_AMAP_PROXY', '30 per minute'), key_func=rate_limit_key)
def amap_proxy(proxy_path):
    """高德 Web 服务代理。

    前端只暴露公开 key，安全码始终在服务端补写。
    """
    safe_path = (proxy_path or '').lstrip('/')
    if not safe_path or '..' in safe_path.split('/'):
        abort(404)
    if safe_path not in ALLOWED_AMAP_PROXY_PATHS:
        abort(404)

    params = [(key, value) for key, value in parse_qsl(request.query_string.decode('utf-8'), keep_blank_values=True) if key != 'jscode']
    security_code = current_app.config.get('AMAP_SECURITY_JS_CODE')
    if security_code:
        params.append(('jscode', security_code))

    try:
        upstream = requests.get(
            f'https://restapi.amap.com/{safe_path}',
            params=params,
            timeout=10
        )
    except requests.RequestException:
        # 上游网络异常统一收口，避免错误细节泄露给前端。
        logger.warning("高德地图上游请求失败")
        abort(502)
    content_type = upstream.headers.get('Content-Type', 'application/json; charset=utf-8')
    if len(upstream.content or b'') > AMAP_PROXY_MAX_BYTES:
        abort(502)
    if 'json' not in content_type.lower():
        abort(502)
    return Response(upstream.content, status=upstream.status_code, content_type=content_type)


@bp.route('/risk', endpoint='public_risk')
def public_risk():
    """公开风险与行动建议"""
    location = sanitize_input(request.args.get('location'), max_length=100)
    return render_public_risk_page(location)


@bp.route('/guest', endpoint='guest_login')
def guest_login():
    """游客模式入口"""
    raw_next = request.args.get('next')
    next_url = str(raw_next)[:200] if raw_next else None
    return handle_guest_login(next_url)


@bp.route('/logout', methods=['POST'], endpoint='logout')
@login_required
def logout():
    """登出"""
    return handle_logout()


@bp.route('/t/<delivery_token>', endpoint='track_delivery')
def track_delivery(delivery_token):
    """Push click tracking endpoint.

    Records click (CTR) then redirects user to the caregiver dashboard (login if needed).
    """
    token = sanitize_input(delivery_token, max_length=80) or ''
    token = token.strip()
    if not token:
        return redirect(url_for('public.index'))

    delivery = AlertDelivery.query.filter_by(delivery_token=token).first()
    if not delivery:
        return redirect(url_for('public.index'))

    try:
        if not delivery.clicked_at:
            clicked_at = utcnow()
            delivery.clicked_at = clicked_at
            # 不明确投递出现有效点击时，可确定消息已经到达，无需继续人工猜测。
            if delivery.status in {'sending', 'uncertain'}:
                delivery.status = 'sent'
                delivery.error = None
                delivery.sent_at = delivery.sent_at or clicked_at
                delivery.reviewed_at = clicked_at
                delivery.review_action = 'click_confirmed'
            db.session.commit()
            log_usage_event(
                'push_click',
                user_id=delivery.user_id,
                pair_id=delivery.pair_id,
                source='web',
                meta={'alert_id': delivery.alert_id, 'channel': delivery.channel},
            )
    except Exception:
        db.session.rollback()

    target = url_for('user.pair_management')
    if current_user.is_authenticated:
        return redirect(target)
    return redirect(url_for('public.login', next=target))


@bp.route('/wxoa', endpoint='wxoa_landing')
def wxoa_landing():
    """WeChat official account landing page (source tracking)."""
    source = sanitize_input(request.args.get('from'), max_length=30) or ''
    article = sanitize_input(request.args.get('article'), max_length=60) or ''
    try:
        log_usage_event(
            'wxoa_land',
            user_id=(current_user.id if current_user.is_authenticated else None),
            source='web',
            meta={'from': source, 'article': article},
        )
    except Exception:
        logger.debug("wxoa_land 埋点写入失败", exc_info=True)
    return render_template('wxoa_landing.html', source=source, article=article)


@bp.route('/about/trust-network', endpoint='about_trust_network')
def about_trust_network():
    """Explain the 'trust network' design logic (thesis loop)."""
    return render_template('about_trust_network.html')
