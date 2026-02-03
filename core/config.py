# -*- coding: utf-8 -*-
"""Application configuration loader."""
import os
import secrets
from pathlib import Path

from config import (
    AI_ALLOWED_MODELS,
    CITY_LOCATION_MAP,
    COMMUNITY_COORDS_GCJ,
    DEFAULT_CITY,
    DEFAULT_LOCATION,
    WEAK_SECRET_KEYWORDS,
)
from core.constants import DEFAULT_CITY_LABEL, WEATHER_CACHE_TTL_MINUTES
from utils.parsers import parse_bool, parse_float, parse_int

QWEATHER_API_BASE_DEFAULT = 'https://mj76x98pfn.re.qweatherapi.com/v7'
SILICONFLOW_API_BASE_DEFAULT = 'https://api.siliconflow.cn/v1'


def _contains_weak_keyword(value):
    if not value:
        return False
    lowered = value.lower()
    return any(keyword in lowered for keyword in WEAK_SECRET_KEYWORDS)


def resolve_database_uri():
    """Resolve database URI from env or local storage."""
    env_uri = (os.getenv('DATABASE_URI') or '').strip()
    repo_root = Path(__file__).resolve().parents[1]
    storage_db = repo_root / 'storage' / 'health_weather.db'
    instance_db = repo_root / 'instance' / 'health_weather.db'

    if env_uri:
        return env_uri
    if storage_db.exists():
        return f"sqlite:///{storage_db.as_posix()}"
    if instance_db.exists():
        return f"sqlite:///{instance_db.as_posix()}"
    return 'sqlite:///health_weather.db'


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

    if not debug_value:
        if not secret_key_env:
            raise RuntimeError(
                "SECRET_KEY 未设置！生产环境必须配置。\n"
                "  生成方式: python3 -c 'import secrets; print(secrets.token_hex(32))'"
            )
        if len(secret_key_env) < 32:
            raise RuntimeError("SECRET_KEY 长度过短，生产环境必须 >= 32 位。")
        if _contains_weak_keyword(secret_key_env):
            raise RuntimeError("SECRET_KEY 包含弱关键词（dev/test/secret 等），请更换随机密钥。")
        if secret_key_env in ('your-secret-key-here', 'your-secret-key-change-in-production'):
            raise RuntimeError("SECRET_KEY 使用了示例值，必须替换为真实的随机密钥！")

        if not pair_token_pepper:
            raise RuntimeError(
                "PAIR_TOKEN_PEPPER 未设置！生产环境必须配置。\n"
                "  生成方式: python3 -c 'import secrets; print(secrets.token_hex(32))'"
            )
        if pair_token_pepper in ('your-pair-token-pepper-here',):
            raise RuntimeError("PAIR_TOKEN_PEPPER 使用了示例值，必须替换为真实的随机密钥！")

    database_uri = resolve_database_uri()
    if database_uri.startswith('sqlite:///'):
        db_path = database_uri.replace('sqlite:///', '')
        if not db_path.startswith('/'):
            db_path = Path(__file__).resolve().parents[1] / db_path
        else:
            db_path = Path(db_path)
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
    amap_key = _normalized_env_value('AMAP_KEY', '')
    amap_security_js_code = _normalized_env_value('AMAP_SECURITY_JS_CODE', '')
    siliconflow_key = _normalized_env_value('SILICONFLOW_API_KEY', '')
    siliconflow_base = _normalized_env_value('SILICONFLOW_API_BASE', SILICONFLOW_API_BASE_DEFAULT)
    pair_token_pepper = _normalized_env_value('PAIR_TOKEN_PEPPER', '')
    demo_mode = os.getenv('DEMO_MODE')
    default_city = _normalized_env_value('DEFAULT_CITY', DEFAULT_CITY)
    default_location = _normalized_env_value('DEFAULT_LOCATION', DEFAULT_LOCATION)
    redis_url = _normalized_env_value('REDIS_URL', '')
    weather_cache_redis_url = _normalized_env_value('WEATHER_CACHE_REDIS_URL', redis_url)

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
    app.config['AMAP_KEY'] = amap_key
    app.config['AMAP_SECURITY_JS_CODE'] = amap_security_js_code
    app.config['SILICONFLOW_API_KEY'] = siliconflow_key
    app.config['SILICONFLOW_API_BASE'] = siliconflow_base
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
    app.config.setdefault('FEATURE_AUDIT_LOGS', parse_bool(os.getenv('FEATURE_AUDIT_LOGS', '0'), default=False))
    app.config.setdefault('FEATURE_STRUCTURED_LOGS', parse_bool(os.getenv('FEATURE_STRUCTURED_LOGS', '1'), default=True))
    app.config.setdefault(
        'FORECAST_CACHE_TTL_MINUTES',
        parse_int(os.getenv('FORECAST_CACHE_TTL_MINUTES', '20'), default=20)
    )
    app.config.setdefault(
        'WEATHER_CACHE_TTL_MINUTES',
        parse_int(os.getenv('WEATHER_CACHE_TTL_MINUTES', str(WEATHER_CACHE_TTL_MINUTES)), default=WEATHER_CACHE_TTL_MINUTES)
    )
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
    app.config.setdefault('RATE_LIMIT_SHORT_CODE', os.getenv('RATE_LIMIT_SHORT_CODE', '3 per hour'))
    app.config.setdefault('RATE_LIMIT_CONFIRM', os.getenv('RATE_LIMIT_CONFIRM', '30 per hour'))
    app.config.setdefault('RATE_LIMIT_HELP', os.getenv('RATE_LIMIT_HELP', '10 per hour'))
    app.config.setdefault('RATE_LIMIT_ESCALATE', os.getenv('RATE_LIMIT_ESCALATE', '10 per hour'))

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

    if not qweather_key:
        logger.warning("QWEATHER_KEY 未配置，天气API将无法使用（可回退 Open-Meteo）。")
    if not amap_key:
        logger.warning("AMAP_KEY 未配置，地图API将无法使用")
    if not amap_security_js_code:
        logger.warning("AMAP_SECURITY_JS_CODE 未配置，地图安全密钥将无法使用")
    if not siliconflow_key:
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
