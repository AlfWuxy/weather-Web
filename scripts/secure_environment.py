#!/usr/bin/env python3
"""环境文件只以私有普通文件读写；秘密通过标准输入传递。"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile


def private_file(path: Path, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise ValueError("环境文件必须是非链接普通文件")
    os.fchmod(fd, 0o600)
    return fd


def read_values(path: Path, create=False):
    with os.fdopen(private_file(path, create), "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    values = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition("=")
        if not sep:
            raise ValueError("环境文件包含无效配置行")
        key, raw = key.strip(), raw.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("环境变量名无效")
        if raw.startswith(('"', "'")):
            quote = raw[0]
            end = raw.find(quote, 1)
            remainder = raw[end + 1:].strip()
            if end < 0 or (remainder and not remainder.startswith("#")):
                raise ValueError("环境变量引号或注释无效")
            raw = raw[1:end]
        else:
            raw = raw.split(" #", 1)[0].strip()
        values[key] = raw
    return lines, values


def write_values(path: Path, updates):
    lines, _ = read_values(path, create=True)
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(value, str):
            raise ValueError("环境变量名或类型无效")
        if any(char in value for char in "\r\n\x00'\\"):
            raise ValueError("环境变量包含不支持的控制字符或引号")
    existing = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=".env-private-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if os.geteuid() == 0:
            os.fchown(fd, existing.st_uid, existing.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for line in lines:
                if line.partition("=")[0].strip() not in updates:
                    handle.write(line + "\n")
            for key, value in updates.items():
                encoded = value if re.fullmatch(r"[a-zA-Z0-9_./:@,+-]*", value) else f"'{value}'"
                handle.write(f"{key}={encoded}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# 仅传递受支持的运行配置，排除部署账号、密码和本地主机定位。
DEPLOY_KEYS = frozenset("""
AI_CONNECT_TIMEOUT
AI_DAILY_LIMIT
AI_DAILY_TOKEN_LIMIT
AI_MAX_TOKENS
AI_MONTHLY_LIMIT
AI_MONTHLY_TOKEN_LIMIT
AI_READ_TIMEOUT
AI_REQUEST_RETRIES
AI_USER_DAILY_LIMIT
AI_USER_DAILY_TOKEN_LIMIT
AMAP_JS_API_KEY
AMAP_SECURITY_JS_CODE
AMAP_WEB_SERVICE_KEY
API_TOKEN_TTL_DAYS
COMMUNITY_RISK_CACHE_LOCK_SECONDS
COMMUNITY_RISK_CACHE_TTL_SECONDS
COMMUNITY_RISK_CACHE_WAIT_SECONDS
COMMUNITY_RISK_PRECOMPUTE_DISEASES
COMMUNITY_RISK_PRECOMPUTE_LOCATIONS
COMMUNITY_RISK_PRECOMPUTE_WINDOW_DAYS
DATABASE_URI
DEFAULT_CITY
DEFAULT_LOCATION
DEMO_MODE
EVENT_META_MAX_BYTES
EVENT_META_MAX_DEPTH
EVENT_META_MAX_FIELDS
FEATURE_API_V1
FEATURE_AUDIT_LOGS
FEATURE_ELDER_MODE
FEATURE_EMERGENCY_TRIAGE
FEATURE_EXPLAIN_OUTPUT
FEATURE_HEAT_EXPOSURE_GIS
FEATURE_NOTIFICATIONS
FEATURE_STRUCTURED_LOGS
FORECAST_CACHE_TTL_MINUTES
GEOCODE_DAILY_LIMIT
GEOCODE_MONTHLY_LIMIT
GEOCODE_RESPONSE_MAX_BYTES
GEOCODE_USER_DAILY_LIMIT
HEAT_EXPOSURE_GIS_UI
HEAT_HOT_DAY_THRESHOLD
LOCATION_CACHE_MAX_ROWS
LOCATION_CACHE_TTL_DAYS
LOGIN_LOCKOUT_SECONDS
LOGIN_MAX_FAILURES
MAX_CONTENT_LENGTH
NOTIFICATION_ESCALATION_DAYS
NOTIFICATION_MAX_DAILY
PAIR_ACTION_TOKEN_TTL_DAYS
PAIR_LIST_PAGE_SIZE
PAIR_MAX_PER_USER
PAIR_TOKEN_PEPPER
PUBLIC_BASE_URL
QWEATHER_API_BASE
QWEATHER_AUTH_MODE
QWEATHER_BUDGET_FAIL_CLOSED
QWEATHER_CANONICAL_LOCATION
QWEATHER_JWT_KID
QWEATHER_JWT_PRIVATE_KEY_PATH
QWEATHER_JWT_PROJECT_ID
QWEATHER_KEY
QWEATHER_MONTHLY_REQUEST_LIMIT
QWEATHER_WARNING_CACHE_TTL_MINUTES
RATE_LIMITS
RATE_LIMIT_AI
RATE_LIMIT_AMAP_PROXY
RATE_LIMIT_CHRONIC
RATE_LIMIT_CONFIRM
RATE_LIMIT_ESCALATE
RATE_LIMIT_FORECAST
RATE_LIMIT_HELP
RATE_LIMIT_LOGIN
RATE_LIMIT_ML
RATE_LIMIT_MP_ALERTS
RATE_LIMIT_MP_EVENTS
RATE_LIMIT_MP_READ
RATE_LIMIT_MP_WRITE
RATE_LIMIT_SHORT_CODE
RATE_LIMIT_STORAGE_URI
RATE_LIMIT_WEATHER
REDIS_URL
SECRET_KEY
SENTRY_DSN
SENTRY_ENVIRONMENT
SENTRY_RELEASE
SENTRY_SEND_PII
SENTRY_TRACES_SAMPLE_RATE
SHORT_CODE_FAIL_MAX
SHORT_CODE_FAIL_WINDOW_MINUTES
SHORT_CODE_LOCK_MINUTES
SHORT_CODE_TTL_DAYS
SILICONFLOW_API_BASE
SILICONFLOW_API_KEY
STATIC_CACHE_MAX_AGE_SECONDS
TIANDITU_TK
TRUSTED_PROXY_CIDRS
WEATHER_CACHE_REDIS_URL
WEATHER_CACHE_TTL_MINUTES
WEATHER_SYNC_LOCATIONS
WXPUSHER_API_BASE
WXPUSHER_APP_TOKEN
""".split())
# 已存在的身份密钥和提供方凭据不隐式轮换，显式轮换应走独立操作。
PRESERVE_KEYS = frozenset(("SECRET_KEY", "PAIR_TOKEN_PEPPER", "QWEATHER_KEY", "AMAP_JS_API_KEY", "AMAP_WEB_SERVICE_KEY", "AMAP_SECURITY_JS_CODE", "WXPUSHER_APP_TOKEN", "SILICONFLOW_API_KEY"))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("protect", "set", "defaults", "payload", "merge"))
    parser.add_argument("path", type=Path)
    parser.add_argument("key", nargs="?")
    args = parser.parse_args()
    if args.mode == "protect":
        os.close(private_file(args.path, create=True))
    elif args.mode == "set":
        write_values(args.path, {args.key: sys.stdin.read()})
    elif args.mode == "defaults":
        _, values = read_values(args.path, create=True)
        write_values(args.path, {key: secrets.token_hex(32) for key in ("SECRET_KEY", "PAIR_TOKEN_PEPPER") if not values.get(key)})
    elif args.mode == "payload":
        values = read_values(args.path)[1] if args.path.exists() else {}
        json.dump({key: os.environ.get(key, values.get(key, "")) for key in DEPLOY_KEYS if key in os.environ or key in values}, sys.stdout)
    elif args.mode == "merge":
        updates = json.load(sys.stdin)
        if not isinstance(updates, dict) or not set(updates) <= DEPLOY_KEYS or not updates.get("PUBLIC_BASE_URL"):
            raise ValueError("部署配置字段不匹配")
        _, current = read_values(args.path, create=True)
        merged = {key: value for key, value in updates.items() if value and (key not in PRESERVE_KEYS or not current.get(key))}
        merged["PUBLIC_BASE_URL"] = updates["PUBLIC_BASE_URL"]
        merged["ALLOW_INSECURE_PUBLIC_BASE_URL"] = "0"
        merged["DEBUG"] = "false"
        merged["FLASK_ENV"] = "production"
        write_values(args.path, merged)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError) as exc:
        print(f"环境文件操作失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
