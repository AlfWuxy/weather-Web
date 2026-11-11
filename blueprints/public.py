# -*- coding: utf-8 -*-
"""Public and auth routes."""
import json
import logging
from pathlib import Path
from urllib.parse import parse_qsl

import requests
from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

logger = logging.getLogger(__name__)

from core.extensions import limiter
from core.extensions import db
from core.resource_budget import (
    ResourceBudgetUnavailable,
    ResourceLimitError,
    UpstreamBusy,
    acquire_lease,
    get_upstream_cache,
    release_lease,
    reserve_upstream,
    upstream_inflight_slot,
)
from core.security import _client_ip_for_rate_limit, hash_identifier, rate_limit_key
from core.time_utils import utcnow
from core.usage import log_usage_event
from core.db_models import AlertDelivery
from services.public_service import (
    render_role_entry,
    handle_login,
    handle_register,
    render_cooling_resources_page,
    render_public_risk_page,
    handle_guest_login,
    handle_logout,
    handle_elder_token_entry,
    _handle_action_lookup,
    _handle_action_confirm,
    _handle_action_help,
    _handle_action_debrief,
    _handle_token_debrief_get,
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


@bp.route('/robots.txt', endpoint='robots_txt')
def robots_txt():
    """允许搜索与 AI 爬虫抓取公开页面。"""
    return Response(ROBOTS_TXT, content_type='text/plain; charset=utf-8')


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
    # URL 不经过 HTML 清理，避免把 & 重复转义为 &amp;。
    raw_next = request.args.get('next') or request.form.get('next')
    next_url = str(raw_next)[:200] if raw_next else None
    return handle_login(next_url)


def _register_ip_key():
    """注册限流按来源 IP 的哈希分桶，不把原始地址写进计数键。"""
    return hash_identifier(_client_ip_for_rate_limit() or 'unknown') or 'unknown'


@bp.route('/register', methods=['GET', 'POST'], endpoint='register')
@limiter.limit(
    lambda: current_app.config.get('RATE_LIMIT_REGISTER_DAILY', '10 per day'),
    methods=['POST'],
    key_func=_register_ip_key,
)
@limiter.limit(
    lambda: current_app.config.get('RATE_LIMIT_REGISTER', '3 per hour'),
    methods=['POST'],
    key_func=_register_ip_key,
)
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
    """带令牌的绑定入口。立刻换成会话票据，地址栏不再保留 token。"""
    return handle_elder_token_entry(token)


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
    return _handle_token_debrief_get(token)


@bp.route('/transparency', endpoint='transparency')
def transparency():
    """透明度说明"""
    return render_template('transparency.html')


PUBLIC_COOLING_CANDIDATE_PATH = Path(__file__).resolve().parents[1] / 'data/cooling_resource_candidates.json'


def _public_cooling_candidates():
    """公开候选预览只保留非医疗公共场所，并删除坐标与来源查询字段。"""
    from services.cooling_candidate_catalog import (
        load_cooling_candidate_catalog,
        public_candidate_previews,
    )

    return public_candidate_previews(
        load_cooling_candidate_catalog(amap_path=PUBLIC_COOLING_CANDIDATE_PATH)
    )


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
        open_only=open_only,
        cooling_candidates=_public_cooling_candidates(),
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

    raw_params = [
        (key, value)
        for key, value in parse_qsl(request.query_string.decode('utf-8'), keep_blank_values=True)
        if key != 'jscode'
    ]
    sorted_params = tuple(sorted(raw_params))
    cache_key = ('amap', safe_path, sorted_params)
    cache = get_upstream_cache()
    ttl = current_app.config.get('AMAP_PROXY_CACHE_TTL_SECONDS', 300)

    def _cached_response():
        cached = cache.get(cache_key)
        if not cached:
            return None
        body, status, content_type = cached
        return Response(body, status=status, content_type=content_type)

    def _release_read_transaction():
        try:
            if db.session.new or db.session.dirty or db.session.deleted:
                return
            db.session.rollback()
        except Exception:
            logger.warning("地图代理读取事务结束失败，已忽略")

    def _map_busy():
        return jsonify({'error': '地图服务繁忙，请稍后再试'}), 429

    hit = _cached_response()
    if hit is not None:
        return hit

    lease = None
    try:
        _release_read_transaction()
        try:
            lease = acquire_lease(repr(cache_key), ttl_seconds=15, namespace='map')
        except (ResourceBudgetUnavailable, ResourceLimitError):
            return _map_busy()
        if not lease:
            hit = _cached_response()
            if hit is not None:
                return hit
            return _map_busy()
        hit = _cached_response()
        if hit is not None:
            return hit

        params = list(raw_params)
        security_code = current_app.config.get('AMAP_SECURITY_JS_CODE')
        if security_code:
            params.append(('jscode', security_code))

        try:
            _release_read_transaction()
            with upstream_inflight_slot('MAP', timeout=2.0):
                reserve_upstream('MAP', units=1)
                upstream = requests.get(
                    f'https://restapi.amap.com/{safe_path}',
                    params=params,
                    timeout=10
                )
        except (UpstreamBusy, ResourceLimitError, ResourceBudgetUnavailable):
            return _map_busy()
        except requests.RequestException:
            # 上游网络异常统一收口，避免错误细节泄露给前端。
            logger.warning("高德地图上游请求失败")
            abort(502)

        content_type = upstream.headers.get('Content-Type', 'application/json; charset=utf-8')
        body = upstream.content or b''
        if len(body) > AMAP_PROXY_MAX_BYTES:
            abort(502)
        if 'json' not in content_type.lower():
            abort(502)
        legal_json = False
        try:
            json.loads(body.decode('utf-8'))
            legal_json = True
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            legal_json = False
        if legal_json and 200 <= upstream.status_code < 300:
            cache.set(cache_key, (bytes(body), upstream.status_code, content_type), ttl)
        return Response(body, status=upstream.status_code, content_type=content_type)
    finally:
        if lease:
            release_lease(lease)


@bp.route('/risk', endpoint='public_risk')
def public_risk():
    """公开风险与行动建议"""
    location = sanitize_input(request.args.get('location'), max_length=100)
    return render_public_risk_page(location)


@bp.route('/guest', endpoint='guest_login')
@limiter.limit('10 per hour', key_func=rate_limit_key)
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
            delivery.clicked_at = utcnow()
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
