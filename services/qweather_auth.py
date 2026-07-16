# -*- coding: utf-8 -*-
"""QWeather 请求认证。

JWT 只在当前进程内短时缓存。私钥、JWT 和认证请求头不得写入日志或 Redis。
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit

import jwt
from flask import current_app, has_app_context


_ALLOWED_AUTH_MODES = {"api_key", "jwt", "disabled"}
_JWT_LIFETIME_SECONDS = 900
_JWT_CLOCK_SKEW_SECONDS = 30
_JWT_REFRESH_MARGIN_SECONDS = 60
_MAX_PRIVATE_KEY_BYTES = 16 * 1024

_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {
    "identity": None,
    "token": None,
    "expires_at": 0,
}


class QWeatherAuthError(RuntimeError):
    """只包含稳定错误码，避免异常文本泄露认证材料。"""


def _value(name: str, config: Optional[Mapping] = None, default=""):
    if config is not None and name in config:
        value = config.get(name)
    elif has_app_context():
        value = current_app.config.get(name, default)
    else:
        value = os.getenv(name, default)
    if isinstance(value, str):
        return value.strip()
    return value if value is not None else default


def get_qweather_auth_mode(config: Optional[Mapping] = None) -> str:
    """返回显式认证模式；旧配置默认兼容 API Key。"""
    mode = str(_value("QWEATHER_AUTH_MODE", config, "") or "").lower()
    if not mode:
        mode = "api_key" if _value("QWEATHER_KEY", config, "") else "disabled"
    if mode not in _ALLOWED_AUTH_MODES:
        raise QWeatherAuthError("qweather_auth_mode_invalid")
    return mode


def is_qweather_configured(config: Optional[Mapping] = None) -> bool:
    """判断当前认证模式是否具备生成请求头所需的配置。"""
    try:
        mode = get_qweather_auth_mode(config)
    except QWeatherAuthError:
        return False
    if mode == "api_key":
        return bool(_value("QWEATHER_KEY", config, ""))
    if mode == "jwt":
        return all(
            (
                _value("QWEATHER_JWT_KID", config, ""),
                _value("QWEATHER_JWT_PROJECT_ID", config, ""),
                _value("QWEATHER_JWT_PRIVATE_KEY_PATH", config, ""),
            )
        )
    return False


def validate_qweather_api_base(
    api_base: str,
    config: Optional[Mapping] = None,
) -> str:
    """阻止认证头发送到非 HTTPS 或非预期的 JWT Host。"""
    value = str(api_base or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise QWeatherAuthError("qweather_api_base_invalid")

    if get_qweather_auth_mode(config) == "jwt":
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname != "qweatherapi.com" and not hostname.endswith(".qweatherapi.com"):
            raise QWeatherAuthError("qweather_jwt_host_invalid")
        if parsed.path.rstrip("/") != "/v7":
            raise QWeatherAuthError("qweather_jwt_base_path_invalid")
    return value


def _private_key_identity(path_value: str, kid: str, project_id: str):
    path = Path(path_value).expanduser()
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise QWeatherAuthError("qweather_jwt_key_unavailable") from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise QWeatherAuthError("qweather_jwt_key_not_regular")
    if file_stat.st_mode & 0o077:
        raise QWeatherAuthError("qweather_jwt_key_permissions")
    if file_stat.st_size <= 0 or file_stat.st_size > _MAX_PRIVATE_KEY_BYTES:
        raise QWeatherAuthError("qweather_jwt_key_size_invalid")

    identity = (
        kid,
        project_id,
        str(path.resolve()),
        file_stat.st_mtime_ns,
        file_stat.st_size,
    )
    return path, identity


def _generate_jwt(path: Path, kid: str, project_id: str, now: int):
    try:
        private_key = path.read_bytes()
        token = jwt.encode(
            {
                "sub": project_id,
                "iat": now - _JWT_CLOCK_SKEW_SECONDS,
                "exp": now + _JWT_LIFETIME_SECONDS,
            },
            private_key,
            algorithm="EdDSA",
            headers={"kid": kid},
        )
    except Exception as exc:
        raise QWeatherAuthError("qweather_jwt_sign_failed") from exc
    if isinstance(token, bytes):
        token = token.decode("ascii")
    if not isinstance(token, str) or not token:
        raise QWeatherAuthError("qweather_jwt_sign_failed")
    return token, now + _JWT_LIFETIME_SECONDS


def _get_cached_jwt(config: Optional[Mapping] = None) -> str:
    kid = str(_value("QWEATHER_JWT_KID", config, "") or "")
    project_id = str(_value("QWEATHER_JWT_PROJECT_ID", config, "") or "")
    key_path = str(_value("QWEATHER_JWT_PRIVATE_KEY_PATH", config, "") or "")
    if not kid or not project_id or not key_path:
        raise QWeatherAuthError("qweather_jwt_config_missing")

    path, identity = _private_key_identity(key_path, kid, project_id)
    now = int(time.time())
    cached_token = _TOKEN_CACHE.get("token")
    if (
        _TOKEN_CACHE.get("identity") == identity
        and cached_token
        and now < int(_TOKEN_CACHE.get("expires_at") or 0) - _JWT_REFRESH_MARGIN_SECONDS
    ):
        return str(cached_token)

    with _TOKEN_LOCK:
        now = int(time.time())
        cached_token = _TOKEN_CACHE.get("token")
        if (
            _TOKEN_CACHE.get("identity") == identity
            and cached_token
            and now < int(_TOKEN_CACHE.get("expires_at") or 0) - _JWT_REFRESH_MARGIN_SECONDS
        ):
            return str(cached_token)
        token, expires_at = _generate_jwt(path, kid, project_id, now)
        _TOKEN_CACHE.update(
            identity=identity,
            token=token,
            expires_at=expires_at,
        )
        return token


def invalidate_qweather_token() -> None:
    """清除当前进程的 JWT 缓存，供轮换密钥或 401 诊断使用。"""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.update(identity=None, token=None, expires_at=0)


def get_qweather_request_headers(
    config: Optional[Mapping] = None,
    api_base: Optional[str] = None,
) -> dict:
    """生成且只生成一种 QWeather 认证请求头。"""
    mode = get_qweather_auth_mode(config)
    if api_base is not None:
        validate_qweather_api_base(api_base, config)
    if mode == "api_key":
        api_key = str(_value("QWEATHER_KEY", config, "") or "")
        if not api_key:
            raise QWeatherAuthError("qweather_api_key_missing")
        return {"X-QW-Api-Key": api_key}
    if mode == "jwt":
        return {"Authorization": f"Bearer {_get_cached_jwt(config)}"}
    raise QWeatherAuthError("qweather_auth_disabled")
