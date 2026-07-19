# -*- coding: utf-8 -*-
"""Application configuration loader."""
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

from config import (
    AI_ALLOWED_MODELS,
    CITY_LOCATION_MAP,
    COMMUNITY_COORDS_GCJ,
    DEFAULT_CITY,
    DEFAULT_LOCATION,
    PUSH_TRACKING_LINK_TTL_DAYS_DEFAULT,
    PUSH_TRACKING_LINK_TTL_DAYS_MAX,
    PUSH_TRACKING_LINK_TTL_DAYS_MIN,
    WEAK_SECRET_KEYWORDS,
)
from core.constants import DEFAULT_CITY_LABEL, WEATHER_CACHE_TTL_MINUTES
from utils.parsers import parse_bool, parse_float, parse_int

QWEATHER_API_BASE_DEFAULT = ''
SILICONFLOW_API_BASE_DEFAULT = 'https://api.siliconflow.cn/v1'
WXPUSHER_API_BASE_DEFAULT = 'https://wxpusher.zjiecode.com/api'
PUBLIC_BASE_URL_FORMAL = 'https://yilaoweather.org'
WXPUSHER_APP_TOKEN_PATTERN = re.compile(r'^AT_[A-Za-z0-9_-]{16,197}$')


def _contains_weak_keyword(value):
    if not value:
        return False
    lowered = value.lower()
    return any(keyword in lowered for keyword in WEAK_SECRET_KEYWORDS)


def _is_memory_storage_uri(uri):
    if not isinstance(uri, str):
        return False
    return uri.strip().lower().startswith('memory://')


def _is_valid_redis_uri(uri):
    """只允许带主机的 Redis 连接地址。"""
    if not isinstance(uri, str) or not uri.strip():
        return False
    try:
        parsed = urlparse(uri.strip())
        return parsed.scheme in {'redis', 'rediss'} and bool(parsed.hostname)
    except ValueError:
        return False


def resolve_database_uri():
    """Resolve database URI from env or local storage."""
    env_uri = (os.getenv('DATABASE_URI') or '').strip()
    repo_root = Path(__file__).resolve().parents[1]
    storage_db = repo_root / 'storage' / 'health_weather.db'
    instance_db = repo_root / 'instance' / 'health_weather.db'

    if env_uri:
        return _normalize_sqlite_uri(env_uri, repo_root)
    if storage_db.exists():
        return f"sqlite:///{storage_db.as_posix()}"
    if instance_db.exists():
        return f"sqlite:///{instance_db.as_posix()}"
    return 'sqlite:///health_weather.db'


def _normalize_sqlite_uri(database_uri, repo_root):
    """Normalize sqlite URIs to avoid Flask-SQLAlchemy "double instance/" paths.

    Flask-SQLAlchemy treats relative sqlite paths as relative to app.instance_path.
    Many configs use DATABASE_URI=sqlite:///instance/xxx.db expecting repo-root-relative,
    which becomes instance/instance/xxx.db at runtime and fails to open.

    We interpret sqlite paths that include subdirectories as repo-root-relative and
    convert them to absolute paths to keep behavior consistent and predictable.
    """
    if not isinstance(database_uri, str):
        return database_uri
    uri = database_uri.strip()
    if not uri:
        return uri

    # Handle common sqlite drivernames; keep any query string intact.
    for scheme in ('sqlite:///', 'sqlite+pysqlite:///'):
        if not uri.startswith(scheme):
            continue
        path_and_query = uri[len(scheme):]
        path_part, sep, query = path_and_query.partition('?')
        if not path_part or path_part == ':memory:':
            return uri
        if path_part.startswith('/'):
            return uri

        # If the path contains a directory, make it absolute relative to repo_root to
        # avoid being re-rooted under Flask's instance_path.
        if '/' in path_part or '\\' in path_part:
            abs_path = (repo_root / path_part).resolve()
            normalized = f"{scheme}{abs_path.as_posix()}"
            if sep:
                normalized += f"?{query}"
            return normalized
        return uri

    return uri


def resolve_sqlite_db_path(database_uri, repo_root=None, instance_dir=None):
    """将 sqlite URI 解析为稳定的本地路径。"""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[1]
    repo_root = Path(repo_root)
    if instance_dir is None:
        instance_dir = repo_root / 'instance'
    instance_dir = Path(instance_dir)
    normalized_uri = _normalize_sqlite_uri(database_uri, repo_root)
    if not isinstance(normalized_uri, str):
        return None

    for scheme in ('sqlite:///', 'sqlite+pysqlite:///'):
        if not normalized_uri.startswith(scheme):
            continue
        path_and_query = normalized_uri[len(scheme):]
        path_part, _, _query = path_and_query.partition('?')
        if not path_part or path_part == ':memory:':
            return None
        if path_part.startswith('/'):
            return Path(path_part)
        # Flask-SQLAlchemy 会把 sqlite 相对路径放到 instance_path 下。
        return (instance_dir / path_part).resolve()

    return None


def resolve_engine_options(database_uri):
    if database_uri.startswith('sqlite'):
        return {}
    return {
        'pool_pre_ping': True,
        'pool_size': 10,
        'max_overflow': 20,
        'pool_recycle': 3600
    }


def validate_production_config():
    """验证生产环境必需的配置项。

    生产环境缺少必需配置时会抛出 RuntimeError。
    """
    errors = []
    debug_value = parse_bool(os.getenv('DEBUG'), default=False)
    secret_key_env = (os.getenv('SECRET_KEY') or '').strip()
    pair_token_pepper = (os.getenv('PAIR_TOKEN_PEPPER') or '').strip()
    rate_limit_storage_env = (os.getenv('RATE_LIMIT_STORAGE_URI') or '').strip()
    redis_url = (os.getenv('REDIS_URL') or '').strip()
    weather_cache_redis_url = (os.getenv('WEATHER_CACHE_REDIS_URL') or '').strip()
    qweather_auth_mode = (os.getenv('QWEATHER_AUTH_MODE') or 'disabled').strip().lower()
    qweather_require_persistent_budget = parse_bool(
        os.getenv('QWEATHER_REQUIRE_PERSISTENT_BUDGET'),
        default=not debug_value,
    )
    wx_miniprogram_values = {
        'WX_MINIPROGRAM_APPID': (os.getenv('WX_MINIPROGRAM_APPID') or '').strip(),
        'WX_MINIPROGRAM_SECRET': (os.getenv('WX_MINIPROGRAM_SECRET') or '').strip(),
        'WX_MINIPROGRAM_OPENID_PEPPER': (os.getenv('WX_MINIPROGRAM_OPENID_PEPPER') or '').strip(),
        'WX_MINIPROGRAM_SESSION_SECRET': (os.getenv('WX_MINIPROGRAM_SESSION_SECRET') or '').strip(),
    }
    wxpusher_app_token = (os.getenv('WXPUSHER_APP_TOKEN') or '').strip()
    feature_wxpusher_env = os.getenv('FEATURE_WXPUSHER')
    feature_wxpusher_raw = (
        feature_wxpusher_env.strip()
        if isinstance(feature_wxpusher_env, str)
        else ''
    )
    # 兼容旧 Web-only 部署：未声明开关但已有 token 时维持原启用行为。
    feature_wxpusher = parse_bool(
        feature_wxpusher_raw,
        default=bool(wxpusher_app_token),
    )
    wxpusher_api_base = (os.getenv('WXPUSHER_API_BASE') or WXPUSHER_API_BASE_DEFAULT).strip()
    public_base_url = (os.getenv('PUBLIC_BASE_URL') or '').strip()
    insecure_public_base_allowed = (os.getenv('ALLOW_INSECURE_PUBLIC_BASE_URL') or '').strip()
    dispatch_lock_path = (os.getenv('DISPATCH_LOCK_PATH') or '').strip()
    feature_web_ai = parse_bool(os.getenv('FEATURE_WEB_AI'), default=False)
    siliconflow_api_key = (os.getenv('SILICONFLOW_API_KEY') or '').strip()

    if not debug_value:
        if feature_wxpusher_raw and feature_wxpusher_raw not in {'0', '1'}:
            raise RuntimeError("FEATURE_WXPUSHER 必须显式设置为 0 或 1。")
        if not secret_key_env:
            raise RuntimeError(
                "SECRET_KEY 未设置！生产环境必须配置。\n"
                "  生成方式: python3 -c 'import secrets; print(secrets.token_hex(32))'"
            )
        if len(secret_key_env) < 32:
            raise RuntimeError("SECRET_KEY 长度过短，生产环境必须 >= 32 位。")
        if _contains_weak_keyword(secret_key_env):
            raise RuntimeError("SECRET_KEY 包含弱关键词（dev/test/secret 等），请更换随机密钥。")
        if secret_key_env in ('your-secret-key-here', 'your-secret-key-change-in-production', 'change-me-min-32-chars'):
            raise RuntimeError("SECRET_KEY 使用了示例值，必须替换为真实的随机密钥！")

        if not pair_token_pepper:
            raise RuntimeError(
                "PAIR_TOKEN_PEPPER 未设置！生产环境必须配置。\n"
                "  生成方式: python3 -c 'import secrets; print(secrets.token_hex(32))'"
            )
        if len(pair_token_pepper) < 32:
            raise RuntimeError("PAIR_TOKEN_PEPPER 长度过短，生产环境必须 >= 32 位。")
        if _contains_weak_keyword(pair_token_pepper):
            raise RuntimeError("PAIR_TOKEN_PEPPER 包含弱关键词，必须更换为独立随机值。")
        if pair_token_pepper in ('your-pair-token-pepper-here', 'change-me-min-32-chars'):
            raise RuntimeError("PAIR_TOKEN_PEPPER 使用了示例值，必须替换为真实的随机密钥！")
        independent_secrets = {
            'PAIR_TOKEN_PEPPER': pair_token_pepper,
            'SECRET_KEY': secret_key_env,
            'WX_MINIPROGRAM_OPENID_PEPPER': wx_miniprogram_values['WX_MINIPROGRAM_OPENID_PEPPER'],
            'WX_MINIPROGRAM_SESSION_SECRET': wx_miniprogram_values['WX_MINIPROGRAM_SESSION_SECRET'],
        }
        configured_secrets = [
            (name, value)
            for name, value in independent_secrets.items()
            if value
        ]
        for index, (name, value) in enumerate(configured_secrets):
            duplicate_name = next(
                (
                    other_name
                    for other_name, other_value in configured_secrets[index + 1:]
                    if secrets.compare_digest(value, other_value)
                ),
                None,
            )
            if duplicate_name:
                # 只报告冲突变量名，不回显任何密钥内容。
                raise RuntimeError(
                    f"{name} 必须与 {duplicate_name} 使用不同的独立随机值。"
                )

        effective_rate_limit_uri = rate_limit_storage_env or redis_url or 'memory://'
        if _is_memory_storage_uri(effective_rate_limit_uri):
            raise RuntimeError(
                "生产环境禁止使用 memory:// 作为限流存储，请配置 REDIS_URL 或 RATE_LIMIT_STORAGE_URI。"
            )

        # Web 可独立运行；一旦启用微信登录，四项服务端材料必须同时存在。
        if any(wx_miniprogram_values.values()):
            missing = [name for name, value in wx_miniprogram_values.items() if not value]
            if missing:
                raise RuntimeError("微信小程序认证配置不完整，缺少: " + ", ".join(missing))
            for name in ('WX_MINIPROGRAM_OPENID_PEPPER', 'WX_MINIPROGRAM_SESSION_SECRET'):
                value = wx_miniprogram_values[name]
                if len(value) < 32 or _contains_weak_keyword(value):
                    raise RuntimeError(f"{name} 必须使用至少 32 位的独立随机值。")
            if public_base_url != PUBLIC_BASE_URL_FORMAL:
                raise RuntimeError("微信正式模式的 PUBLIC_BASE_URL 必须使用固定正式 origin。")
            if insecure_public_base_allowed:
                raise RuntimeError("微信正式模式禁止 ALLOW_INSECURE_PUBLIC_BASE_URL。")
        if not feature_wxpusher and wxpusher_app_token:
            raise RuntimeError("FEATURE_WXPUSHER=0 时必须清空 WXPUSHER_APP_TOKEN。")

        # WxPusher 也可能在 Web-only 运行时开启，启用后必须锁定正式跳转与官方 API。
        if feature_wxpusher:
            if not wxpusher_app_token:
                raise RuntimeError("FEATURE_WXPUSHER=1 时必须配置 WXPUSHER_APP_TOKEN。")
            if public_base_url != PUBLIC_BASE_URL_FORMAL:
                raise RuntimeError("启用 WxPusher 时 PUBLIC_BASE_URL 必须使用固定正式 origin。")
            if insecure_public_base_allowed:
                raise RuntimeError("启用 WxPusher 时禁止 ALLOW_INSECURE_PUBLIC_BASE_URL。")
            if wxpusher_api_base != WXPUSHER_API_BASE_DEFAULT:
                raise RuntimeError("启用 WxPusher 时 WXPUSHER_API_BASE 必须使用固定官方 origin。")
            if not WXPUSHER_APP_TOKEN_PATTERN.fullmatch(wxpusher_app_token):
                raise RuntimeError("WXPUSHER_APP_TOKEN 格式或长度异常。")

        if any(wx_miniprogram_values.values()) or feature_wxpusher:
            if not dispatch_lock_path or not Path(dispatch_lock_path).is_absolute():
                raise RuntimeError("微信或 WxPusher 正式运行时必须配置绝对 DISPATCH_LOCK_PATH。")

        # 正式站暂不处理用户自由文本，也不允许遗留第三方 AI 凭据。
        if feature_web_ai or siliconflow_api_key:
            raise RuntimeError("正式环境必须关闭 FEATURE_WEB_AI 并清空 SILICONFLOW_API_KEY。")

        if qweather_auth_mode in {'api_key', 'jwt'}:
            persistent_budget_uri = weather_cache_redis_url or redis_url
            if not qweather_require_persistent_budget:
                raise RuntimeError(
                    "正式 QWeather 必须启用 QWEATHER_REQUIRE_PERSISTENT_BUDGET。"
                )
            if not _is_valid_redis_uri(persistent_budget_uri):
                raise RuntimeError(
                    "正式 QWeather 必须配置有效的 REDIS_URL 或 "
                    "WEATHER_CACHE_REDIS_URL。"
                )

    database_uri = resolve_database_uri()
    db_path = resolve_sqlite_db_path(database_uri, Path(__file__).resolve().parents[1])
    if db_path is not None:
        db_dir = db_path.parent
        if not db_dir.exists():
            try:
                db_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                errors.append(f"无法创建数据库目录 {db_dir}: {exc}")

    if errors:
        raise RuntimeError(
            "配置验证失败，无法启动应用：\n" +
            "\n".join(f"  - {err}" for err in errors) +
            "\n\n请参考 .env.example 文件配置环境变量。"
        )


def _configure_sentry(app, logger):
    dsn = app.config.get('SENTRY_DSN')
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        logger.warning("SENTRY_DSN 已配置但 sentry-sdk 未安装，已跳过初始化。")
        return

    try:
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=app.config.get('SENTRY_TRACES_SAMPLE_RATE', 0.0),
            environment=app.config.get('SENTRY_ENVIRONMENT') or None,
            release=app.config.get('SENTRY_RELEASE') or None,
            send_default_pii=app.config.get('SENTRY_SEND_PII', False)
        )
        logger.info("Sentry 已初始化")
    except Exception as exc:
        logger.warning("Sentry 初始化失败: %s", exc)


def _normalized_env_value(key, default=None):
    value = os.getenv(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def configure_app(app, logger):
    """Load configuration into the Flask app."""
    secret_key_is_generated = False

    debug_value = parse_bool(os.getenv('DEBUG'), default=False)
    secret_key_env = _normalized_env_value('SECRET_KEY')
    qweather_key = _normalized_env_value('QWEATHER_KEY', '')
    qweather_api_base = _normalized_env_value('QWEATHER_API_BASE', QWEATHER_API_BASE_DEFAULT)
    qweather_auth_mode = _normalized_env_value('QWEATHER_AUTH_MODE', '').lower()
    if not qweather_auth_mode:
        qweather_auth_mode = 'api_key' if qweather_key else 'disabled'
    if qweather_auth_mode not in {'api_key', 'jwt', 'disabled'}:
        raise RuntimeError("QWEATHER_AUTH_MODE 必须是 api_key、jwt 或 disabled。")
    qweather_jwt_kid = _normalized_env_value('QWEATHER_JWT_KID', '')
    qweather_jwt_project_id = _normalized_env_value('QWEATHER_JWT_PROJECT_ID', '')
    qweather_jwt_private_key_path = _normalized_env_value('QWEATHER_JWT_PRIVATE_KEY_PATH', '')
    amap_key = _normalized_env_value('AMAP_KEY', '')
    amap_security_js_code = _normalized_env_value('AMAP_SECURITY_JS_CODE', '')
    siliconflow_key = _normalized_env_value('SILICONFLOW_API_KEY', '')
    siliconflow_base = _normalized_env_value('SILICONFLOW_API_BASE', SILICONFLOW_API_BASE_DEFAULT)
    feature_web_ai = parse_bool(os.getenv('FEATURE_WEB_AI'), default=False)
    wxpusher_app_token = _normalized_env_value('WXPUSHER_APP_TOKEN', '')
    feature_wxpusher = parse_bool(
        os.getenv('FEATURE_WXPUSHER'),
        default=bool(wxpusher_app_token),
    )
    wxpusher_api_base = _normalized_env_value('WXPUSHER_API_BASE', WXPUSHER_API_BASE_DEFAULT)
    push_tracking_link_ttl_days = max(
        PUSH_TRACKING_LINK_TTL_DAYS_MIN,
        min(
            parse_int(
                os.getenv(
                    'PUSH_TRACKING_LINK_TTL_DAYS',
                    str(PUSH_TRACKING_LINK_TTL_DAYS_DEFAULT),
                ),
                default=PUSH_TRACKING_LINK_TTL_DAYS_DEFAULT,
            ),
            PUSH_TRACKING_LINK_TTL_DAYS_MAX,
        ),
    )
    wx_miniprogram_appid = _normalized_env_value('WX_MINIPROGRAM_APPID', '')
    wx_miniprogram_secret = _normalized_env_value('WX_MINIPROGRAM_SECRET', '')
    wx_miniprogram_openid_pepper = _normalized_env_value('WX_MINIPROGRAM_OPENID_PEPPER', '')
    wx_miniprogram_session_secret = _normalized_env_value('WX_MINIPROGRAM_SESSION_SECRET', '')
    wx_miniprogram_privacy_version = _normalized_env_value(
        'WX_MINIPROGRAM_PRIVACY_VERSION',
        '2026-07-18',
    )
    analytics_test_user_ids = _normalized_env_value('ANALYTICS_TEST_USER_IDS', '')
    analytics_min_location_count = max(
        3,
        min(
            parse_int(
                os.getenv('ANALYTICS_MIN_LOCATION_COUNT', '3'),
                default=3,
            ),
            1000,
        ),
    )
    public_base_url = _normalized_env_value('PUBLIC_BASE_URL', '')
    dispatch_lock_path = _normalized_env_value('DISPATCH_LOCK_PATH', '')
    pair_token_pepper = _normalized_env_value('PAIR_TOKEN_PEPPER', '')
    demo_mode = os.getenv('DEMO_MODE')
    default_city = _normalized_env_value('DEFAULT_CITY', DEFAULT_CITY)
    default_location = _normalized_env_value('DEFAULT_LOCATION', DEFAULT_LOCATION)
    qweather_canonical_location = _normalized_env_value(
        'QWEATHER_CANONICAL_LOCATION',
        default_location,
    )
    redis_url = _normalized_env_value('REDIS_URL', '')
    weather_cache_redis_url = _normalized_env_value('WEATHER_CACHE_REDIS_URL', redis_url)
    qweather_require_persistent_budget = parse_bool(
        os.getenv('QWEATHER_REQUIRE_PERSISTENT_BUDGET'),
        default=not debug_value,
    )

    validate_production_config()

    database_uri = resolve_database_uri()
    app.config['SQLALCHEMY_DATABASE_URI'] = database_uri
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = resolve_engine_options(database_uri)

    app.config['DEBUG'] = bool(debug_value)

    secret_key = secret_key_env
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
        secret_key_is_generated = True
        logger.warning("SECRET_KEY 未配置，已使用临时随机值；生产环境请设置 SECRET_KEY。")

    app.config['SECRET_KEY'] = secret_key
    app.config['QWEATHER_KEY'] = qweather_key
    app.config['QWEATHER_API_BASE'] = qweather_api_base
    app.config['QWEATHER_AUTH_MODE'] = qweather_auth_mode
    app.config['QWEATHER_REQUIRE_PERSISTENT_BUDGET'] = qweather_require_persistent_budget
    app.config['QWEATHER_JWT_KID'] = qweather_jwt_kid
    app.config['QWEATHER_JWT_PROJECT_ID'] = qweather_jwt_project_id
    app.config['QWEATHER_JWT_PRIVATE_KEY_PATH'] = qweather_jwt_private_key_path
    app.config['QWEATHER_CANONICAL_LOCATION'] = qweather_canonical_location
    app.config['AMAP_KEY'] = amap_key
    app.config['AMAP_SECURITY_JS_CODE'] = amap_security_js_code
    app.config['SILICONFLOW_API_KEY'] = siliconflow_key
    app.config['SILICONFLOW_API_BASE'] = siliconflow_base
    app.config['FEATURE_WEB_AI'] = feature_web_ai
    app.config['FEATURE_WXPUSHER'] = feature_wxpusher
    app.config['WXPUSHER_APP_TOKEN'] = wxpusher_app_token
    app.config['WXPUSHER_API_BASE'] = wxpusher_api_base
    app.config['PUSH_TRACKING_LINK_TTL_DAYS'] = push_tracking_link_ttl_days
    app.config['WX_MINIPROGRAM_APPID'] = wx_miniprogram_appid
    app.config['WX_MINIPROGRAM_SECRET'] = wx_miniprogram_secret
    app.config['WX_MINIPROGRAM_OPENID_PEPPER'] = wx_miniprogram_openid_pepper
    app.config['WX_MINIPROGRAM_SESSION_SECRET'] = wx_miniprogram_session_secret
    app.config['WX_MINIPROGRAM_PRIVACY_VERSION'] = wx_miniprogram_privacy_version
    app.config['ANALYTICS_TEST_USER_IDS'] = analytics_test_user_ids
    app.config['ANALYTICS_MIN_LOCATION_COUNT'] = analytics_min_location_count
    app.config['WX_MINIPROGRAM_SESSION_TTL_SECONDS'] = max(
        300,
        min(
            parse_int(os.getenv('WX_MINIPROGRAM_SESSION_TTL_SECONDS', '604800'), default=604800),
            2592000,
        ),
    )
    app.config['WX_MINIPROGRAM_MAX_ACTIVE_SESSIONS'] = max(
        1,
        min(
            parse_int(os.getenv('WX_MINIPROGRAM_MAX_ACTIVE_SESSIONS', '5'), default=5),
            20,
        ),
    )
    app.config['WX_MINIPROGRAM_AUTH_TIMEOUT'] = max(
        2.0,
        min(parse_float(os.getenv('WX_MINIPROGRAM_AUTH_TIMEOUT', '8'), default=8.0), 15.0),
    )
    app.config['PUBLIC_BASE_URL'] = public_base_url
    app.config['DISPATCH_LOCK_PATH'] = dispatch_lock_path
    app.config['WECHAT_FORMAL_RUNTIME'] = bool(
        not debug_value
        and wx_miniprogram_appid
        and wx_miniprogram_secret
        and wx_miniprogram_openid_pepper
        and wx_miniprogram_session_secret
    )
    app.config['PREFERRED_URL_SCHEME'] = 'https' if not app.config['DEBUG'] else 'http'
    app.config['SESSION_COOKIE_SECURE'] = not app.config['DEBUG']
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['REMEMBER_COOKIE_SECURE'] = not app.config['DEBUG']
    app.config['REMEMBER_COOKIE_HTTPONLY'] = True
    app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
    app.config['MAX_CONTENT_LENGTH'] = max(
        65536,
        min(
            parse_int(os.getenv('MAX_CONTENT_LENGTH_BYTES', '1048576'), default=1048576),
            8 * 1024 * 1024,
        ),
    )
    app.config['AI_ALLOWED_MODELS'] = AI_ALLOWED_MODELS
    app.config['DEFAULT_CITY'] = default_city or DEFAULT_CITY_LABEL
    app.config['DEFAULT_LOCATION'] = default_location or DEFAULT_LOCATION
    app.config['CITY_LOCATION_MAP'] = CITY_LOCATION_MAP
    app.config['COMMUNITY_COORDS_GCJ'] = COMMUNITY_COORDS_GCJ
    app.config.setdefault('AI_CONNECT_TIMEOUT', parse_float(os.getenv('AI_CONNECT_TIMEOUT', '8'), default=8.0))
    app.config.setdefault('AI_READ_TIMEOUT', parse_float(os.getenv('AI_READ_TIMEOUT', '60'), default=60.0))
    app.config.setdefault('AI_REQUEST_RETRIES', parse_int(os.getenv('AI_REQUEST_RETRIES', '1'), default=1))
    app.config.setdefault('AI_MAX_TOKENS', parse_int(os.getenv('AI_MAX_TOKENS', '800'), default=800))
    app.config.setdefault('DEMO_MODE', parse_bool(demo_mode, default=False))
    app.config.setdefault('FEATURE_API_V1', parse_bool(os.getenv('FEATURE_API_V1', '1'), default=True))
    app.config.setdefault('FEATURE_EXPLAIN_OUTPUT', parse_bool(os.getenv('FEATURE_EXPLAIN_OUTPUT', '0'), default=False))
    app.config.setdefault('FEATURE_EMERGENCY_TRIAGE', parse_bool(os.getenv('FEATURE_EMERGENCY_TRIAGE', '0'), default=False))
    app.config.setdefault('FEATURE_ELDER_MODE', parse_bool(os.getenv('FEATURE_ELDER_MODE', '0'), default=False))
    app.config.setdefault('FEATURE_NOTIFICATIONS', parse_bool(os.getenv('FEATURE_NOTIFICATIONS', '0'), default=False))
    app.config.setdefault('FEATURE_HEAT_EXPOSURE_GIS', parse_bool(os.getenv('FEATURE_HEAT_EXPOSURE_GIS', '0'), default=False))
    app.config.setdefault('FEATURE_AUDIT_LOGS', parse_bool(os.getenv('FEATURE_AUDIT_LOGS', '0'), default=False))
    app.config.setdefault('FEATURE_STRUCTURED_LOGS', parse_bool(os.getenv('FEATURE_STRUCTURED_LOGS', '1'), default=True))
    app.config.setdefault('TRUSTED_PROXY_CIDRS', os.getenv('TRUSTED_PROXY_CIDRS', '127.0.0.1/32,::1/128'))
    app.config.setdefault(
        'FORECAST_CACHE_TTL_MINUTES',
        max(parse_int(os.getenv('FORECAST_CACHE_TTL_MINUTES', '30'), default=30), 10)
    )
    app.config.setdefault(
        'WEATHER_CACHE_TTL_MINUTES',
        max(
            parse_int(
                os.getenv('WEATHER_CACHE_TTL_MINUTES', str(WEATHER_CACHE_TTL_MINUTES)),
                default=WEATHER_CACHE_TTL_MINUTES,
            ),
            10,
        )
    )
    app.config.setdefault(
        'QWEATHER_WARNING_CACHE_TTL_MINUTES',
        max(parse_int(os.getenv('QWEATHER_WARNING_CACHE_TTL_MINUTES', '30'), default=30), 10)
    )
    app.config.setdefault(
        'QWEATHER_MONTHLY_REQUEST_LIMIT',
        max(parse_int(os.getenv('QWEATHER_MONTHLY_REQUEST_LIMIT', '40000'), default=40000), 0)
    )
    app.config.setdefault(
        'QWEATHER_BUDGET_FAIL_CLOSED',
        parse_bool(os.getenv('QWEATHER_BUDGET_FAIL_CLOSED', '1'), default=True)
    )
    app.config.setdefault(
        'QWEATHER_NETWORK_NOT_BEFORE_EPOCH',
        _normalized_env_value('QWEATHER_NETWORK_NOT_BEFORE_EPOCH', '')
    )
    if qweather_auth_mode != 'disabled' and not qweather_api_base:
        logger.warning("QWEATHER_API_BASE 未配置，将跳过 QWeather Host 相关调用并使用兜底链路。")
    if qweather_auth_mode == 'api_key' and not qweather_key:
        logger.warning("QWEATHER_AUTH_MODE=api_key 但 QWEATHER_KEY 未配置，QWeather 不会生效。")
    if qweather_auth_mode == 'jwt':
        missing_jwt_fields = [
            name
            for name, value in (
                ('QWEATHER_JWT_KID', qweather_jwt_kid),
                ('QWEATHER_JWT_PROJECT_ID', qweather_jwt_project_id),
                ('QWEATHER_JWT_PRIVATE_KEY_PATH', qweather_jwt_private_key_path),
            )
            if not value
        ]
        if missing_jwt_fields:
            logger.warning("QWeather JWT 配置不完整，缺少: %s", ', '.join(missing_jwt_fields))
    app.config.setdefault('NOTIFICATION_ESCALATION_DAYS', parse_int(os.getenv('NOTIFICATION_ESCALATION_DAYS', '3'), default=3))
    app.config.setdefault('NOTIFICATION_MAX_DAILY', parse_int(os.getenv('NOTIFICATION_MAX_DAILY', '5'), default=5))
    app.config.setdefault('HEAT_HOT_DAY_THRESHOLD', parse_float(os.getenv('HEAT_HOT_DAY_THRESHOLD', '35'), default=35.0))
    app.config.setdefault('RATE_LIMITS', os.getenv('RATE_LIMITS', '200 per minute'))

    rate_limit_storage_env = _normalized_env_value('RATE_LIMIT_STORAGE_URI')
    if rate_limit_storage_env:
        rate_limit_storage_default = rate_limit_storage_env
    elif redis_url:
        rate_limit_storage_default = redis_url
    else:
        rate_limit_storage_default = 'memory://'
    app.config.setdefault('RATE_LIMIT_STORAGE_URI', rate_limit_storage_default)
    app.config.setdefault('REDIS_URL', redis_url)
    app.config.setdefault('WEATHER_CACHE_REDIS_URL', weather_cache_redis_url)

    app.config.setdefault('RATE_LIMIT_WEATHER', os.getenv('RATE_LIMIT_WEATHER', app.config['RATE_LIMITS']))
    app.config.setdefault('RATE_LIMIT_FORECAST', os.getenv('RATE_LIMIT_FORECAST', app.config['RATE_LIMITS']))
    app.config.setdefault('RATE_LIMIT_CHRONIC', os.getenv('RATE_LIMIT_CHRONIC', app.config['RATE_LIMITS']))
    app.config.setdefault('RATE_LIMIT_ML', os.getenv('RATE_LIMIT_ML', app.config['RATE_LIMITS']))
    app.config.setdefault('RATE_LIMIT_AI', os.getenv('RATE_LIMIT_AI', '20 per minute'))
    app.config.setdefault('RATE_LIMIT_LOGIN', os.getenv('RATE_LIMIT_LOGIN', '5 per 5 minutes'))
    app.config.setdefault('LOGIN_MAX_FAILURES', parse_int(os.getenv('LOGIN_MAX_FAILURES', '5'), default=5))
    app.config.setdefault('LOGIN_LOCKOUT_SECONDS', parse_int(os.getenv('LOGIN_LOCKOUT_SECONDS', '300'), default=300))
    app.config.setdefault('RATE_LIMIT_SHORT_CODE', os.getenv('RATE_LIMIT_SHORT_CODE', '3 per hour'))
    app.config.setdefault('RATE_LIMIT_CONFIRM', os.getenv('RATE_LIMIT_CONFIRM', '30 per hour'))
    app.config.setdefault('RATE_LIMIT_HELP', os.getenv('RATE_LIMIT_HELP', '10 per hour'))
    app.config.setdefault('RATE_LIMIT_ESCALATE', os.getenv('RATE_LIMIT_ESCALATE', '10 per hour'))
    app.config.setdefault('RATE_LIMIT_AMAP_PROXY', os.getenv('RATE_LIMIT_AMAP_PROXY', '30 per minute'))
    app.config.setdefault('RATE_LIMIT_MP_READ', os.getenv('RATE_LIMIT_MP_READ', '120 per minute'))
    app.config.setdefault('RATE_LIMIT_MP_WRITE', os.getenv('RATE_LIMIT_MP_WRITE', '30 per minute'))
    app.config.setdefault('RATE_LIMIT_MP_ALERTS', os.getenv('RATE_LIMIT_MP_ALERTS', '30 per minute'))
    app.config.setdefault('RATE_LIMIT_MP_EVENTS', os.getenv('RATE_LIMIT_MP_EVENTS', '60 per minute'))
    app.config.setdefault('RATE_LIMIT_MP_AUTH', os.getenv('RATE_LIMIT_MP_AUTH', '10 per 5 minutes'))
    app.config.setdefault('RATE_LIMIT_MP_PUBLIC', os.getenv('RATE_LIMIT_MP_PUBLIC', '600 per minute'))
    app.config.setdefault(
        'MINIPROGRAM_SNAPSHOT_RETENTION',
        max(
            2,
            min(
                parse_int(os.getenv('MINIPROGRAM_SNAPSHOT_RETENTION', '96'), default=96),
                1000,
            ),
        ),
    )
    app.config.setdefault(
        'API_TOKEN_TTL_DAYS',
        max(1, min(parse_int(os.getenv('API_TOKEN_TTL_DAYS', '30'), default=30), 365)),
    )
    app.config.setdefault('PAIR_ACTION_TOKEN_TTL_DAYS', parse_int(os.getenv('PAIR_ACTION_TOKEN_TTL_DAYS', '90'), default=90))
    app.config.setdefault('SHORT_CODE_TTL_DAYS', parse_int(os.getenv('SHORT_CODE_TTL_DAYS', '90'), default=90))

    if not app.config['DEBUG'] and app.config['RATE_LIMIT_STORAGE_URI'].startswith('memory://'):
        logger.warning(
            "RATE_LIMIT_STORAGE_URI uses memory://; set REDIS_URL or RATE_LIMIT_STORAGE_URI for multi-process safety."
        )

    if pair_token_pepper:
        app.config['PAIR_TOKEN_PEPPER'] = pair_token_pepper
    elif secret_key and not secret_key_is_generated:
        app.config['PAIR_TOKEN_PEPPER'] = secret_key
        logger.warning("PAIR_TOKEN_PEPPER 未配置，已使用 SECRET_KEY 作为 pepper。建议设置独立的 PAIR_TOKEN_PEPPER。")
    else:
        pepper_path = os.path.join(app.instance_path, 'pair_token_pepper.txt')
        try:
            with open(pepper_path, 'r', encoding='utf-8') as handle:
                pair_token_pepper = handle.read().strip()
        except FileNotFoundError:
            pair_token_pepper = ''
        except OSError as exc:
            pair_token_pepper = ''
            logger.warning("读取 pepper 文件失败: %s", exc)

        if not pair_token_pepper:
            pair_token_pepper = secrets.token_urlsafe(32)
            try:
                os.makedirs(app.instance_path, exist_ok=True)
                with open(pepper_path, 'w', encoding='utf-8') as handle:
                    handle.write(pair_token_pepper)
                try:
                    os.chmod(pepper_path, 0o600)
                except OSError as exc:
                    logger.debug("Unable to chmod pepper file: %s", exc)
                logger.warning("PAIR_TOKEN_PEPPER 未配置，已生成并持久化随机值至 %s。", pepper_path)
            except OSError as exc:
                logger.warning("写入 pepper 文件失败: %s", exc)
        else:
            logger.warning("PAIR_TOKEN_PEPPER 未配置，已从 %s 读取持久化值。", pepper_path)

        app.config['PAIR_TOKEN_PEPPER'] = pair_token_pepper

    app.config.setdefault('SHORT_CODE_FAIL_MAX', parse_int(os.getenv('SHORT_CODE_FAIL_MAX', '5'), default=5))
    app.config.setdefault(
        'SHORT_CODE_FAIL_WINDOW_MINUTES',
        parse_int(os.getenv('SHORT_CODE_FAIL_WINDOW_MINUTES', '30'), default=30)
    )
    app.config.setdefault(
        'SHORT_CODE_LOCK_MINUTES',
        parse_int(os.getenv('SHORT_CODE_LOCK_MINUTES', '30'), default=30)
    )

    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    if not app.config.get('SQLALCHEMY_DATABASE_URI', '').startswith('sqlite'):
        app.config.setdefault('SQLALCHEMY_ENGINE_OPTIONS', {
            'pool_pre_ping': True,
            'pool_size': 10,
            'pool_recycle': 3600,
            'max_overflow': 20
        })
    else:
        app.config.pop('SQLALCHEMY_ENGINE_OPTIONS', None)

    if qweather_auth_mode == 'disabled':
        logger.warning("QWeather 已禁用，天气API将使用 Open-Meteo 或规则兜底。")
    if not amap_key:
        logger.warning("AMAP_KEY 未配置，地图API将无法使用")
    if not amap_security_js_code:
        logger.warning("AMAP_SECURITY_JS_CODE 未配置，地图安全密钥将无法使用")
    if not feature_web_ai:
        logger.info("FEATURE_WEB_AI=0，Web AI 问答已关闭")
    elif not siliconflow_key:
        logger.warning("SILICONFLOW_API_KEY 未配置，AI问答将不可用")

    app.config.setdefault('SENTRY_DSN', _normalized_env_value('SENTRY_DSN', ''))
    app.config.setdefault('SENTRY_ENVIRONMENT', _normalized_env_value('SENTRY_ENVIRONMENT', ''))
    app.config.setdefault('SENTRY_RELEASE', _normalized_env_value('SENTRY_RELEASE', ''))
    app.config.setdefault(
        'SENTRY_TRACES_SAMPLE_RATE',
        parse_float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0'), default=0.0)
    )
    app.config.setdefault('SENTRY_SEND_PII', parse_bool(os.getenv('SENTRY_SEND_PII', '0'), default=False))

    _configure_sentry(app, logger)

    # Static caching (safe with template-level cache busting via url_for(..., v=...)).
    app.config.setdefault(
        'STATIC_CACHE_MAX_AGE_SECONDS',
        parse_int(os.getenv('STATIC_CACHE_MAX_AGE_SECONDS', '2592000'), default=2592000)  # 30 days
    )
