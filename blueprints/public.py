# -*- coding: utf-8 -*-
"""Public and auth routes."""
import json
import logging
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

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
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from config import (
    PUSH_TRACKING_LINK_TTL_DAYS_DEFAULT,
    PUSH_TRACKING_LINK_TTL_DAYS_MAX,
    PUSH_TRACKING_LINK_TTL_DAYS_MIN,
)

logger = logging.getLogger(__name__)

from core.extensions import limiter
from core.extensions import db
from core.audit import log_audit
from core.guest import is_guest_user
from core.security import (
    rate_limit_key,
    registration_rate_limit_key,
)
from core.time_utils import ensure_utc_aware, utcnow
from core.usage import log_usage_event
from core.db_models import AlertDelivery, WeatherAlert
from core.time_utils import today_local
from services.public_service import (
    render_role_entry,
    handle_login,
    handle_register,
    handle_account_link_code,
    handle_account_link_phone,
    render_account_link_page,
    render_cooling_resources_page,
    render_public_risk_page,
    handle_guest_login,
    handle_logout,
    _handle_action_lookup,
    _handle_action_confirm,
    _handle_action_help,
    _handle_action_debrief,
    _formal_web_actions_are_read_only,
    _resolve_pair_from_session_or_code,
    _validate_pair_token_binding,
    _build_action_context,
    _resolve_action_routes,
    _render_action_page
)
from services.push.dispatch import DELIVERY_LOCAL_FAILURES
from services.heat_exposure_gis_service import (
    PUBLIC_GEOJSON_PATH,
    PUBLIC_GEOJSON_SHA256,
    load_validated_public_geojson,
)
from utils.validators import sanitize_input

bp = Blueprint('public', __name__)
SEO_FALLBACK_BASE_URL = 'https://yilaoweather.org'
PUBLIC_CONTENT_SIGNAL = 'ai-train=no, search=yes, ai-input=yes'
PUBLIC_SITEMAP_PATHS = (
    '/',
    '/risk',
    '/cooling',
    '/duchang-heat-vulnerability-map',
    '/transparency',
    '/about/trust-network',
)
PUBLIC_HEAT_GEOJSON_PATH = PUBLIC_GEOJSON_PATH
PUBLIC_COOLING_CANDIDATE_PATH = (
    Path(__file__).resolve().parent.parent
    / 'data'
    / 'cooling_resource_candidates.json'
)
ROBOTS_TXT = """User-agent: *
Content-Signal: ai-train=no, search=yes, ai-input=yes
Allow: /
Allow: /llms.txt
Disallow: /admin
Disallow: /api/
Disallow: /mp/api/
Disallow: /dashboard
Disallow: /caregiver
Disallow: /community
Disallow: /community-risk
Disallow: /healthz
Disallow: /logout
Disallow: /profile
Disallow: /account-link
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


def _trusted_seo_base_url():
    """SEO 地址只能来自受信配置，绝不采用请求 Host。"""
    configured = str(
        current_app.config.get('PUBLIC_BASE_URL') or ''
    ).strip().rstrip('/')
    parsed = urlsplit(configured)
    if (
        parsed.scheme == 'https'
        and parsed.netloc
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and parsed.path in ('', '/')
        and not parsed.query
        and not parsed.fragment
    ):
        return configured
    return SEO_FALLBACK_BASE_URL


@lru_cache(maxsize=8)
def _read_public_preview_json(path_value, mtime_ns):
    """按文件版本缓存公开数据，返回值仍需由调用方裁剪。"""
    del mtime_ns
    return json.loads(Path(path_value).read_text(encoding='utf-8'))


def _read_versioned_public_json(path):
    """读取项目内公开 JSON；修改后会因 mtime 自动失效。"""
    stat_result = path.stat()
    return _read_public_preview_json(str(path), stat_result.st_mtime_ns)


def _public_heat_preview_summary():
    """仅提取县域聚合统计，不把完整网格或内部路径写入 HTML。"""
    if not current_app.config.get('FEATURE_HEAT_EXPOSURE_GIS'):
        return {'available': False}
    try:
        collection = load_validated_public_geojson(PUBLIC_HEAT_GEOJSON_PATH)
        metadata = collection.get('metadata') or {}
        quality = metadata.get('quality_summary') or {}
        spatial = metadata.get('spatial_definition') or {}
        layers = metadata.get('layers') or {}
        study_period = metadata.get('study_period') or {}
        age_layer = layers.get('age65_share_pct') or {}
        temperature_layer = layers.get('q3_lst_c_mean') or {}
        if (
            collection.get('type') != 'FeatureCollection'
            or quality.get('independent_validation') != 'pass'
            or quality.get('hard_failures') != 0
        ):
            raise ValueError('public_heat_preview_not_validated')
        summary = {
            'available': True,
            'cell_count': int(spatial.get('county_center_cells') or 0),
            'positive_population_cells': int(
                spatial.get('positive_population_support_cells') or 0
            ),
            'age65_median': float(age_layer['median']),
            'age65_min': float(age_layer['min']),
            'age65_max': float(age_layer['max']),
            'temperature_median': float(temperature_layer['median']),
            'temperature_min': float(temperature_layer['min']),
            'temperature_max': float(temperature_layer['max']),
            'study_start': str(study_period.get('start') or ''),
            'study_end': str(study_period.get('end') or ''),
            'generated_at': str(metadata.get('generated_at_utc') or ''),
        }
        if summary['cell_count'] <= 0:
            raise ValueError('public_heat_preview_empty')
        return summary
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        logger.exception('公开热暴露预览聚合数据读取失败')
        return {'available': False}


def _public_cooling_candidates():
    """公开候选预览只保留非医疗公共场所，并删除坐标与来源查询字段。"""
    try:
        payload = _read_versioned_public_json(PUBLIC_COOLING_CANDIDATE_PATH)
        if (
            payload.get('publication_status') != 'candidate_only'
            or payload.get('coordinate_system') != 'GCJ-02'
        ):
            raise ValueError('public_cooling_candidate_contract_invalid')
        role_labels = {
            'cooling_candidate': '候选公共纳凉场所',
            'service_candidate': '候选志愿服务点',
        }
        category_labels = {
            'public_culture': '公共文化场所',
            'community_service': '社区服务场所',
            'volunteer_service': '志愿服务组织',
        }
        candidates = []
        for item in payload.get('items') or []:
            if not isinstance(item, dict):
                continue
            role = item.get('public_role')
            category = item.get('category')
            if (
                role not in role_labels
                or category not in category_labels
                or item.get('verification_status')
                != 'pending_human_verification'
                or item.get('is_active') is not False
            ):
                continue
            candidates.append({
                'name': str(item.get('name') or '').strip()[:80],
                'address': str(item.get('address') or '').strip()[:160],
                'opening_hours_hint': str(
                    item.get('opening_hours_hint') or ''
                ).strip()[:160],
                'role_label': role_labels[role],
                'category_label': category_labels[category],
            })
        return [item for item in candidates if item['name']][:12]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        logger.exception('公开避暑候选预览读取失败')
        return []


def _push_tracking_ttl_days():
    """读取已经由应用配置层收敛的推送链接有效期。"""
    value = current_app.config.get(
        'PUSH_TRACKING_LINK_TTL_DAYS',
        PUSH_TRACKING_LINK_TTL_DAYS_DEFAULT,
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = PUSH_TRACKING_LINK_TTL_DAYS_DEFAULT
    return max(
        PUSH_TRACKING_LINK_TTL_DAYS_MIN,
        min(parsed, PUSH_TRACKING_LINK_TTL_DAYS_MAX),
    )


def _push_tracking_anchor(delivery):
    """优先使用发送时间，旧记录仅回退到更早的预警创建时间。"""
    candidate = delivery.sent_at
    if candidate is None:
        candidate = (
            db.session.query(WeatherAlert.alert_date)
            .filter(WeatherAlert.id == delivery.alert_id)
            .scalar()
        )
    if not isinstance(candidate, datetime):
        return None
    try:
        return ensure_utc_aware(candidate)
    except (OverflowError, ValueError):
        return None

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
    body = (
        f"{ROBOTS_TXT.rstrip()}\n"
        f"Sitemap: {_trusted_seo_base_url()}/sitemap.xml\n"
    )
    response = Response(body, content_type='text/plain; charset=utf-8')
    response.headers['Cache-Control'] = 'public, max-age=300'
    response.headers['Content-Signal'] = PUBLIC_CONTENT_SIGNAL
    return response


@bp.route('/sitemap.xml', endpoint='sitemap_xml')
def sitemap_xml():
    """只公开固定匿名内容页，登录态与个人页面不进入站点地图。"""
    base_url = _trusted_seo_base_url()
    url_rows = ''.join(
        f'<url><loc>{escape(base_url + path)}</loc></url>'
        for path in PUBLIC_SITEMAP_PATHS
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f'{url_rows}</urlset>'
    )
    response = Response(
        body,
        content_type='application/xml; charset=utf-8',
    )
    response.headers['Cache-Control'] = 'public, max-age=3600'
    response.headers['Content-Signal'] = PUBLIC_CONTENT_SIGNAL
    return response


@bp.route('/llms.txt', endpoint='llms_txt')
def llms_txt():
    """提供实验性 AI 发现摘要，不把它宣称为正式网络标准。"""
    base_url = _trusted_seo_base_url()
    public_pages = (
        ('主页', '/'),
        ('公开天气风险与行动建议', '/risk'),
        ('已核验避暑资源', '/cooling'),
        ('都昌县热暴露与老年人口脆弱性聚合地图',
         '/duchang-heat-vulnerability-map'),
        ('指标透明度', '/transparency'),
        ('信任网络说明', '/about/trust-network'),
    )
    page_rows = '\n'.join(
        f'- [{label}]({base_url}{path})'
        for label, path in public_pages
    )
    body = (
        '# 宜老天气通\n\n'
        '> 面向都昌县老人、家属和社区的天气风险行动服务。'
        '本文件是实验性 AI 发现摘要，不代表正式或通用的网络标准。\n\n'
        '## Public pages\n\n'
        f'{page_rows}\n\n'
        '## Discovery\n\n'
        f'- [Sitemap]({base_url}/sitemap.xml)\n'
        f'- [Robots policy]({base_url}/robots.txt)\n\n'
        '## Privacy boundary\n\n'
        '- 只抓取上方公开、县域聚合或方法说明页面。\n'
        '- 不抓取登录后页面、管理后台、API、家庭与照护关系、'
        '社区私密工作区、手机号、微信身份、绑定码或用户精确位置。\n'
        '- 地表温度不是气温、体感温度或个人医疗风险评分；'
        '候选地点必须完成人工核验后才会进入正式资源页。\n\n'
        'English note: The map contains de-identified, modeled ~1 km '
        'research grids within Duchang County, not county-level totals '
        'or household records. Private user and community data must not '
        'be crawled.\n'
    )
    response = Response(body, content_type='text/plain; charset=utf-8')
    response.headers['Cache-Control'] = 'public, max-age=3600'
    response.headers['Content-Signal'] = PUBLIC_CONTENT_SIGNAL
    response.headers['X-Robots-Tag'] = 'index, follow'
    return response


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
    response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return response


@bp.route(
    '/data/duchang-heat-exposure.geojson',
    endpoint='public_heat_geojson',
)
def public_heat_geojson():
    """通过冻结摘要与完整白名单校验后提供公开研究网格。"""
    if not current_app.config.get('FEATURE_HEAT_EXPOSURE_GIS'):
        abort(404)
    try:
        load_validated_public_geojson(PUBLIC_HEAT_GEOJSON_PATH)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        logger.exception('公开热暴露 GeoJSON 发布校验失败')
        abort(404)
    response = send_file(
        PUBLIC_HEAT_GEOJSON_PATH,
        mimetype='application/geo+json',
        as_attachment=False,
        download_name='duchang_heat_exposure_cells.geojson',
        conditional=True,
        etag=PUBLIC_GEOJSON_SHA256,
        max_age=86_400,
    )
    response.headers['Cache-Control'] = 'public, max-age=86400, immutable'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
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
@limiter.limit(
    lambda: current_app.config.get(
        'RATE_LIMIT_REGISTER_ATTEMPTS',
        '30 per hour',
    ),
    methods=['POST'],
    key_func=registration_rate_limit_key,
)
def register():
    """注册"""
    return handle_register()


@bp.route('/account-link', methods=['GET'], endpoint='account_link')
@login_required
def account_link():
    """网页与微信小程序账号串联的最小页面。"""
    if is_guest_user(current_user):
        return redirect(url_for('public.register'), code=303)
    return render_account_link_page()


@bp.route('/account-link/phone', methods=['POST'], endpoint='account_link_phone')
@login_required
@limiter.limit('10 per hour', key_func=rate_limit_key)
def account_link_phone():
    """保存待验证手机号标识。"""
    if is_guest_user(current_user):
        return redirect(url_for('public.register'), code=303)
    return handle_account_link_phone()


@bp.route('/account-link/code', methods=['POST'], endpoint='account_link_code')
@login_required
@limiter.limit(
    lambda: current_app.config.get('RATE_LIMIT_ACCOUNT_LINK', '5 per hour'),
    key_func=rate_limit_key,
)
def account_link_code():
    """复验当前密码后生成一次性小程序绑定码。"""
    if is_guest_user(current_user):
        return redirect(url_for('public.register'), code=303)
    return handle_account_link_code()


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
    if _formal_web_actions_are_read_only():
        # 正式微信态直接复用固定停用页，避免读取短码、配对和健康行动数据。
        return _handle_action_lookup(
            token=token,
            entry_action=url_for('public.elder_enter'),
        )

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
        open_only=open_only,
        cooling_candidates=_public_cooling_candidates(),
    )


@bp.route(
    '/duchang-heat-vulnerability-map',
    endpoint='heat_vulnerability_preview',
)
def heat_vulnerability_preview():
    """公开县域热暴露与脆弱性聚合预览，不读取任何个人数据。"""
    summary = _public_heat_preview_summary()
    candidates = _public_cooling_candidates()
    gis_data_url = (
        url_for(
            'public.public_heat_geojson',
            v=PUBLIC_GEOJSON_SHA256[:16],
        )
        if summary.get('available')
        else None
    )

    canonical_url = (
        f'{_trusted_seo_base_url()}/duchang-heat-vulnerability-map'
    )
    cacheable_anonymous = _is_cacheable_anonymous_home()
    template_context = {
        'heat_summary': summary,
        'cooling_candidates': candidates,
        'gis_data_url': gis_data_url,
        'seo_base_url': _trusted_seo_base_url(),
        'seo_canonical_url': canonical_url,
        'seo_robots': 'index, follow',
    }
    if cacheable_anonymous:
        # 匿名公开页无需创建 CSRF Session，便于搜索爬虫稳定抓取。
        template_context['csrf_token'] = lambda: ''

    response = make_response(render_template(
        'heat_vulnerability_preview.html',
        **template_context,
    ))
    response.headers['X-Robots-Tag'] = 'index, follow'
    if cacheable_anonymous and not session.modified:
        response.headers['Cache-Control'] = (
            'public, max-age=900, stale-while-revalidate=300'
        )
        response.headers['Cloudflare-CDN-Cache-Control'] = (
            'public, max-age=900, stale-while-revalidate=300'
        )
        session.accessed = False
    else:
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Cloudflare-CDN-Cache-Control'] = 'no-store'
    return response


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


@bp.route('/logout', methods=['GET', 'POST'], endpoint='logout')
@login_required
def logout():
    """展示退出确认页，并在用户主动提交后撤销会话。"""
    return handle_logout()


@bp.route('/t/<delivery_token>', methods=['GET', 'POST'], endpoint='track_delivery')
@limiter.limit("30 per minute", key_func=rate_limit_key)
def track_delivery(delivery_token):
    """展示确认页，并仅在用户主动提交后记录首次送达确认。"""
    token = sanitize_input(delivery_token, max_length=80) or ''
    token = token.strip()
    if not token:
        return redirect(url_for('public.index'))

    delivery = AlertDelivery.query.filter_by(delivery_token=token).first()
    if not delivery:
        return redirect(url_for('public.index'))

    checked_at = ensure_utc_aware(utcnow())
    tracking_anchor = _push_tracking_anchor(delivery)
    if tracking_anchor is None:
        return redirect(url_for('public.index'))
    try:
        expires_at = tracking_anchor + timedelta(days=_push_tracking_ttl_days())
    except OverflowError:
        return redirect(url_for('public.index'))
    if checked_at < tracking_anchor or checked_at > expires_at:
        return redirect(url_for('public.index'))

    if request.method in {'GET', 'HEAD'}:
        # GET/HEAD 可能由微信预览、浏览器预取或安全扫描触发，严禁在此写入送达事实。
        response = make_response(
            render_template(
                'push_delivery_confirm.html',
                delivery_token=token,
            )
        )
        response.headers['Cache-Control'] = 'no-store, private, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
        return response

    clicked_at = checked_at

    try:
        # 条件更新让并发或重复提交只产生一次主动确认事实与一次分析事件。
        first_click = (
            AlertDelivery.query.filter(
                AlertDelivery.id == delivery.id,
                AlertDelivery.clicked_at.is_(None),
            ).update(
                {AlertDelivery.clicked_at: clicked_at},
                synchronize_session=False,
            )
            == 1
        )
        if first_click:
            # 主动确认是强送达证据；CAS 不覆盖并发或既有人工复核结论。
            promotable_failed = db.and_(
                AlertDelivery.status == 'failed',
                db.or_(
                    AlertDelivery.error.is_(None),
                    AlertDelivery.error.notin_(tuple(DELIVERY_LOCAL_FAILURES)),
                ),
            )
            db.session.execute(
                db.update(AlertDelivery)
                .where(
                    AlertDelivery.id == delivery.id,
                    AlertDelivery.clicked_at == clicked_at,
                    AlertDelivery.review_action.is_(None),
                    AlertDelivery.reviewed_at.is_(None),
                    db.or_(
                        AlertDelivery.status.in_(('sending', 'uncertain', 'retry_ready')),
                        promotable_failed,
                    ),
                )
                .values(
                    status='sent',
                    error=None,
                    sent_at=db.func.coalesce(AlertDelivery.sent_at, tracking_anchor),
                    reviewed_at=clicked_at,
                    review_action='click_confirmed',
                )
            )
            log_audit(
                'push_delivery_user_confirmed',
                resource_type='alert_delivery',
                resource_id=delivery.id,
                metadata={
                    'previous_status': str(delivery.status or ''),
                    'previous_review_action': delivery.review_action,
                },
            )
            db.session.commit()
            db.session.expire(delivery)
            db.session.refresh(delivery)
            log_usage_event(
                'push_click',
                user_id=delivery.user_id,
                pair_id=delivery.pair_id,
                source='web',
                meta={'alert_id': delivery.alert_id, 'channel': delivery.channel},
            )
        else:
            db.session.rollback()
    except Exception:
        db.session.rollback()
        logger.debug("推送点击记录失败", exc_info=True)

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
