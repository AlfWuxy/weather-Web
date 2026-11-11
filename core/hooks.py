# -*- coding: utf-8 -*-
"""Request/response hooks and template helpers."""
import ipaddress
import json
import logging
import os
import secrets
import stat
import time
import zlib
from datetime import datetime
from pathlib import Path

from flask import abort, g, jsonify, request, send_file, session, url_for as flask_url_for
from flask_login import current_user
from werkzeug.security import safe_join

from core.metric_explanations import (
    get_metric_explanation_groups,
    get_metric_explanations,
)
from core.security import csrf_failure_response, generate_csrf_token, validate_csrf
from core.weather import (
    get_location_options,
    get_user_location_value,
    normalize_location_name
)

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 10 * 1024
MAX_JSON_DEPTH = 5


def _redact_sensitive_path(path):
    """结构化日志不得记录行动链接或点击追踪 token。"""
    path = str(path or '')
    for prefix in ('/e/', '/t/'):
        if not path.startswith(prefix):
            continue
        remainder = path[len(prefix):]
        suffix = ''
        if '/' in remainder:
            _, tail = remainder.split('/', 1)
            suffix = f'/{tail}'
        return f'{prefix}<token>{suffix}'
    return path


def _exceeds_json_depth(value, max_depth, current_depth=1):
    if current_depth > max_depth:
        return True
    if isinstance(value, dict):
        for item in value.values():
            if _exceeds_json_depth(item, max_depth, current_depth + 1):
                return True
    elif isinstance(value, list):
        for item in value:
            if _exceeds_json_depth(item, max_depth, current_depth + 1):
                return True
    return False


def _valid_key_length(value):
    return isinstance(value, str) and 20 <= len(value) <= 100


_SENSITIVE_BLUEPRINTS = frozenset({
    'admin', 'user', 'health', 'analysis', 'tools', 'api', 'mp_api', 'guest_experience',
})
_SENSITIVE_PATH_ROOTS = ('/login', '/register', '/logout', '/guest', '/action', '/elder')
_SENSITIVE_SESSION_KEYS = (
    '_user_id',
    'guest_id',
    'pair_session_id',
    'pair_action_grant',
    'pair_credential_reveal',
    'last_api_token_plain',
)


def _has_symlink_component(static_root: str, rel_filename: str) -> tuple[bool, str | None]:
    """检查静态根或相对路径的每一段是否为符号链接。

    只对路径本身做 lstat，发现软链后立即返回，绝不向链接目标继续走。
    safe_join 拒绝的路径也视为不可读取。
    """
    root = Path(static_root)
    try:
        if root.is_symlink():
            return True, os.fspath(root)
    except OSError:
        return True, os.fspath(root)

    try:
        target = safe_join(os.fspath(static_root), rel_filename)
    except (TypeError, ValueError):
        return True, None
    if not target:
        return True, None

    relative = os.path.relpath(target, os.fspath(static_root))
    if relative == os.curdir:
        return False, None
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return True, None

    cursor = root
    for part in relative.split(os.sep):
        if part in ('', os.curdir):
            continue
        if part == os.pardir:
            return True, None
        cursor = cursor / part
        try:
            if cursor.is_symlink():
                return True, os.fspath(cursor)
        except OSError:
            return True, os.fspath(cursor)
    return False, None


def _remote_addr_is_trusted_proxy():
    """直接对端是否落在受信代理网段内，不解析 X-Forwarded-For。"""
    remote_addr = request.remote_addr
    if not remote_addr:
        return False
    try:
        address = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    try:
        from core.audit import _trusted_proxy_networks
        networks = _trusted_proxy_networks()
    except Exception:
        logger.debug("HSTS 受信代理网段读取失败", exc_info=True)
        return False
    try:
        return any(address in network for network in networks)
    except TypeError:
        return False


def _request_qualifies_for_hsts(app):
    if not app.config.get('HSTS_ENABLED'):
        return False
    if request.is_secure:
        return True
    # 只信任最右一跳。代理追加时左端可被客户端伪造；Nginx 单值 https 也落在末项。
    forwarded_proto = request.headers.get('X-Forwarded-Proto', '')
    if forwarded_proto.split(',')[-1].strip().lower() != 'https':
        return False
    return _remote_addr_is_trusted_proxy()


def _set_hsts_header(response, app):
    try:
        max_age = int(app.config.get('HSTS_MAX_AGE_SECONDS', 0))
    except (TypeError, ValueError):
        return
    if max_age < 0:
        return
    directive = f'max-age={max_age}'
    if app.config.get('HSTS_INCLUDE_SUBDOMAINS'):
        directive += '; includeSubDomains'
    response.headers['Strict-Transport-Security'] = directive


def _path_is_sensitive(path):
    if path.startswith(('/e/', '/t/')):
        return True
    for root in _SENSITIVE_PATH_ROOTS:
        if path == root or path.startswith(root + '/'):
            return True
    return False


def _session_has_sensitive_identity():
    """只探测已打开的会话，成员判断不会把 accessed 标成真。"""
    try:
        return any(key in session for key in _SENSITIVE_SESSION_KEYS)
    except Exception:
        logger.debug("读取会话身份键失败，按敏感响应处理", exc_info=True)
        return True


def _response_sets_or_refreshes_csrf_cookie(app):
    """本响应会写出或续期承载 CSRF 的会话 cookie。

    CSRF 存在 Flask 会话里。成员判断和 dict.get 不会把 accessed 标成真，
    避免给本来可缓存、且这次没有改会话的响应加上 Vary。
    """
    try:
        if '_csrf_token' not in session:
            return False
        if session.modified:
            return True
        refresh = app.config.get('SESSION_REFRESH_EACH_REQUEST', True)
        if not refresh:
            return False
        return bool(dict.get(session, '_permanent', False))
    except Exception:
        logger.debug("读取 CSRF 会话状态失败，按敏感响应处理", exc_info=True)
        return True


def _current_user_is_authenticated(app):
    """已加载用户、记住登录或本请求已读过会话时，才真正访问 current_user。"""
    if '_login_user' in g:
        user = g._login_user
        try:
            return bool(getattr(user, 'is_authenticated', False))
        except Exception:
            return True

    remember_name = app.config.get('REMEMBER_COOKIE_NAME', 'remember_token')
    try:
        session_was_used = bool(session.accessed or session.modified)
    except Exception:
        return True
    if remember_name not in request.cookies and not session_was_used:
        return False
    try:
        if current_user.is_authenticated:
            return True
    except Exception:
        return True
    return _session_has_sensitive_identity()


def _response_must_not_be_cached(app):
    if request.blueprint in _SENSITIVE_BLUEPRINTS:
        return True
    if _path_is_sensitive(request.path or ''):
        return True
    if _response_sets_or_refreshes_csrf_cookie(app):
        return True
    if _session_has_sensitive_identity():
        return True
    return _current_user_is_authenticated(app)


def _sanitize_log_value(value):
    """把日志文本里的行分隔符和控制字符转成可见转义，避免拆行。

    上限按转义膨胀留足，正常结构化日志不会被截断。
    """
    from utils.log_safety import sanitize_log_text

    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return sanitize_log_text(text, max_length=max(len(text) * 10, 1))


def register_hooks(app):
    """Register app hooks, filters, and context processors."""
    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({'success': False, 'error': 'request_too_large'}), 413

    @app.before_request
    def enforce_request_body_limit():
        # 先检查长度，CSRF 或业务代码不得先解析超大请求体。
        maximum = app.config.get('MAX_CONTENT_LENGTH', 1024 * 1024)
        if request.content_length is not None and request.content_length > maximum:
            from werkzeug.exceptions import RequestEntityTooLarge
            raise RequestEntityTooLarge()
        if request.environ.get('wsgi.input_terminated') and not request.environ.get('CONTENT_LENGTH'):
            # 分块上传无 Content-Length，额外读取一个哨兵字节区分恰好上限与超限。
            import io
            from werkzeug.exceptions import RequestEntityTooLarge
            body = request.environ['wsgi.input'].read(maximum + 1)
            if len(body) > maximum:
                raise RequestEntityTooLarge()
            request.environ['wsgi.input'] = io.BytesIO(body)
            request.environ['CONTENT_LENGTH'] = str(len(body))

    @app.before_request
    def init_request_context():
        """初始化请求上下文（结构化日志使用）"""
        g.request_id = request.headers.get('X-Request-Id') or secrets.token_hex(8)
        g.request_start = time.perf_counter()
        g.external_api_timings = []

    @app.before_request
    def csrf_protect():
        """统一CSRF校验"""
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            # MiniProgram API uses Bearer token auth and must not require CSRF.
            if request.path.startswith('/mp/api/'):
                return None
            if not validate_csrf():
                return csrf_failure_response()

    @app.after_request
    def apply_security_response_headers(response):
        """最后运行的响应头。Flask 按注册逆序执行，本钩子第一个注册。"""
        if _request_qualifies_for_hsts(app):
            _set_hsts_header(response, app)

        # 静态资源维持 send_file 原有的长缓存，不被后面的禁缓存规则覆盖。
        if request.endpoint == 'static' or (request.path or '').startswith('/static/'):
            return response

        path = request.path or ''
        if path.startswith('/e/') or path.startswith('/t/'):
            response.headers['Referrer-Policy'] = 'no-referrer'
        if getattr(g, 'clear_site_data_on_logout', False):
            response.headers['Clear-Site-Data'] = '"cache", "storage"'
        if _response_must_not_be_cached(app):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            response.vary.add('Cookie')
        return response

    @app.after_request
    def log_request(response):
        """结构化请求日志"""
        if not app.config.get('FEATURE_STRUCTURED_LOGS'):
            return response
        try:
            duration_ms = None
            if hasattr(g, 'request_start'):
                duration_ms = round((time.perf_counter() - g.request_start) * 1000, 2)
            user_id = None
            user_role = None
            if current_user.is_authenticated:
                user_id = getattr(current_user, 'id', None)
                user_role = getattr(current_user, 'role', None)
            request_id = getattr(g, 'request_id', None)
            if isinstance(request_id, str):
                # X-Request-Id 可被客户端夹带 U+2028/U+2029 或控制字符拆行。
                request_id = _sanitize_log_value(request_id)
            log_payload = {
                'request_id': request_id,
                'user_id': user_id,
                'user_role': user_role,
                'method': request.method,
                'path': _redact_sensitive_path(request.path),
                'endpoint': request.endpoint,
                'status': response.status_code,
                'duration_ms': duration_ms,
                'external_api': getattr(g, 'external_api_timings', [])
            }
            logger.info(_sanitize_log_value(json.dumps(log_payload, ensure_ascii=False)))
            if getattr(g, 'request_id', None):
                response.headers['X-Request-Id'] = g.request_id
        except Exception as exc:
            logger.debug("结构化日志失败: %s", exc)
        return response

    @app.template_filter('from_json')
    def from_json_filter(value):
        """将JSON字符串转换为Python对象"""
        if not value:
            return []
        raw = value if isinstance(value, (str, bytes, bytearray)) else str(value)
        raw_bytes = raw.encode('utf-8') if isinstance(raw, str) else raw
        if len(raw_bytes) > MAX_JSON_BYTES:
            logger.warning("JSON payload too large: %s bytes", len(raw_bytes))
            return []
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("JSON parse failed: %s, value: %s", exc, str(value)[:100])
            return []
        if _exceeds_json_depth(parsed, MAX_JSON_DEPTH):
            logger.warning("JSON depth exceeds %s", MAX_JSON_DEPTH)
            return []
        return parsed

    def _versioned_static_url_for(endpoint, **values):
        """Append a cache-busting `v=` parameter for static assets.

        This allows us to set longer cache headers on /static without risking
        stale assets after deployments.
        """
        if endpoint == 'static':
            filename = values.get('filename')
            static_root = app.static_folder
            if filename and static_root:
                try:
                    has_symlink, _link_path = _has_symlink_component(static_root, filename)
                    if not has_symlink:
                        file_path = safe_join(os.fspath(static_root), filename)
                        if file_path:
                            values['v'] = int(os.stat(file_path, follow_symlinks=False).st_mtime)
                except OSError:
                    # If file missing (e.g. optional assets), fall back to plain url_for.
                    pass
        return flask_url_for(endpoint, **values)

    @app.context_processor
    def override_url_for():
        return dict(url_for=_versioned_static_url_for)

    @app.context_processor
    def inject_now():
        """注入当前时间到模板"""
        current_location = normalize_location_name(get_user_location_value())
        payload = {
            'now': lambda: datetime.now(tz=__import__('zoneinfo').ZoneInfo('Asia/Shanghai')),
            'csrf_token': generate_csrf_token,
            'current_location': current_location,
            'location_options': get_location_options(),
            'ai_models': app.config.get('AI_ALLOWED_MODELS', []),
            'metric_explanations': get_metric_explanations(),
            'metric_explanation_groups': get_metric_explanation_groups(),
            'feature_flags': {
                'explain_output': app.config.get('FEATURE_EXPLAIN_OUTPUT'),
                'emergency_triage': app.config.get('FEATURE_EMERGENCY_TRIAGE'),
                'elder_mode': app.config.get('FEATURE_ELDER_MODE'),
                'notifications': app.config.get('FEATURE_NOTIFICATIONS'),
                'heat_exposure_gis': app.config.get('FEATURE_HEAT_EXPOSURE_GIS')
            }
        }
        map_endpoints = {'user.community_risk', 'public.cooling_resources'}
        map_paths = {'/community-risk', '/cooling'}
        needs_map_keys = request.endpoint in map_endpoints or request.path in map_paths
        if needs_map_keys:
            amap_key = app.config.get('AMAP_JS_API_KEY', '')
            amap_code = app.config.get('AMAP_SECURITY_JS_CODE', '')
            if _valid_key_length(amap_key):
                payload['amap_key'] = amap_key
            elif amap_key:
                logger.warning("Invalid AMAP_JS_API_KEY length; skipping template injection")
            if _valid_key_length(amap_code):
                payload['amap_security_js_code'] = amap_code
            elif amap_code:
                logger.warning("Invalid AMAP_SECURITY_JS_CODE length; skipping template injection")
        return payload

    app.jinja_env.globals['csrf_token'] = generate_csrf_token

    static_max_age = app.config.get('STATIC_CACHE_MAX_AGE_SECONDS', 0)
    try:
        static_max_age = int(static_max_age) if static_max_age is not None else 0
    except (TypeError, ValueError):
        static_max_age = 0

    if static_max_age > 0:
        def _get_send_file_max_age(filename):  # noqa: ANN001 - Flask hook signature
            # Apply only to Flask's built-in /static endpoint.
            if (request.path or '').startswith('/static/'):
                return static_max_age
            return None
        # Instance override (Flask uses this hook when serving static and send_file).
        app.get_send_file_max_age = _get_send_file_max_age

    def _send_static_file_nofollow(filename):
        """发送静态文件。路径中任一软链都拒绝，打开时也不跟随最后一段。"""
        static_root = app.static_folder
        if not static_root or not filename:
            abort(404)
        has_symlink, _link_path = _has_symlink_component(static_root, filename)
        if has_symlink:
            abort(404)
        full_path = safe_join(os.fspath(static_root), filename)
        if not full_path:
            abort(404)

        fd = None
        stream = None
        try:
            fd = os.open(full_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                abort(404)
            stream = os.fdopen(fd, 'rb')
            fd = None
            response = send_file(
                stream,
                download_name=os.path.basename(filename),
                conditional=True,
                etag=f"{st.st_mtime}-{st.st_size}-{zlib.adler32(full_path.encode()) & 0xFFFFFFFF}",
                last_modified=st.st_mtime,
                max_age=app.get_send_file_max_age(filename),
            )
            if response.status_code == 200:
                response.content_length = st.st_size
            stream = None
            return response
        except OSError:
            abort(404)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    app.send_static_file = _send_static_file_nofollow
    if 'static' in app.view_functions:
        app.view_functions['static'] = _send_static_file_nofollow

    # 蓝图和 Flask-Login 可能更早注册 after_request。把本钩子固定到队首，
    # 逆序执行时它最后运行，缓存与 HSTS 不会被后续钩子改掉。
    handlers = app.after_request_funcs.get(None)
    if isinstance(handlers, list):
        for index, handler in enumerate(handlers):
            if getattr(handler, '__name__', None) == 'apply_security_response_headers':
                if index != 0:
                    handlers.insert(0, handlers.pop(index))
                break
