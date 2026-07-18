# -*- coding: utf-8 -*-
"""QWeather 请求认证。

JWT 只在当前进程内短时缓存。私钥、JWT 和认证请求头不得写入日志或 Redis。
"""

from __future__ import annotations

import errno
import hashlib
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
    """阻止任一认证模式把凭据发送到非官方 QWeather 端点。"""
    value = str(api_base or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise QWeatherAuthError("qweather_api_base_invalid")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname != "qweatherapi.com" and not hostname.endswith(".qweatherapi.com"):
        raise QWeatherAuthError("qweather_jwt_host_invalid")
    if parsed.path.rstrip("/") != "/v7":
        raise QWeatherAuthError("qweather_jwt_base_path_invalid")
    return value


def _file_fingerprint(file_stat):
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _read_private_key_snapshot(path_value: str, kid: str, project_id: str):
    """安全读取私钥固定快照，并返回可感知内容轮换的缓存身份。"""
    path = Path(path_value).expanduser()
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise QWeatherAuthError("qweather_jwt_key_unavailable")

    flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        file_descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR}:
            raise QWeatherAuthError("qweather_jwt_key_not_regular") from exc
        raise QWeatherAuthError("qweather_jwt_key_unavailable") from exc

    try:
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise QWeatherAuthError("qweather_jwt_key_not_regular")
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise QWeatherAuthError("qweather_jwt_key_permissions")
        if before.st_size <= 0 or before.st_size > _MAX_PRIVATE_KEY_BYTES:
            raise QWeatherAuthError("qweather_jwt_key_size_invalid")

        chunks = []
        total = 0
        while True:
            chunk = os.read(
                file_descriptor,
                min(8192, _MAX_PRIVATE_KEY_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_PRIVATE_KEY_BYTES:
                raise QWeatherAuthError("qweather_jwt_key_size_invalid")
        after = os.fstat(file_descriptor)
        try:
            current_path_stat = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise QWeatherAuthError("qweather_jwt_key_changed") from exc
    except QWeatherAuthError:
        raise
    except OSError as exc:
        raise QWeatherAuthError("qweather_jwt_key_unavailable") from exc
    finally:
        os.close(file_descriptor)

    if (
        total != before.st_size
        or _file_fingerprint(before) != _file_fingerprint(after)
        or not stat.S_ISREG(current_path_stat.st_mode)
        or stat.S_IMODE(current_path_stat.st_mode) != 0o600
        or _file_fingerprint(current_path_stat) != _file_fingerprint(after)
    ):
        raise QWeatherAuthError("qweather_jwt_key_changed")

    private_key = b"".join(chunks)
    identity = (
        kid,
        project_id,
        os.path.abspath(os.fspath(path)),
        before.st_dev,
        before.st_ino,
        hashlib.sha256(private_key).digest(),
    )
    return private_key, identity


def _generate_jwt(private_key: bytes, kid: str, project_id: str, now: int):
    try:
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

    with _TOKEN_LOCK:
        # 读取、比较和更新缓存共用同一把锁，避免轮换期间旧快照覆盖新令牌。
        private_key, identity = _read_private_key_snapshot(key_path, kid, project_id)
        now = int(time.time())
        cached_token = _TOKEN_CACHE.get("token")
        if (
            _TOKEN_CACHE.get("identity") == identity
            and cached_token
            and now < int(_TOKEN_CACHE.get("expires_at") or 0) - _JWT_REFRESH_MARGIN_SECONDS
        ):
            return str(cached_token)
        token, expires_at = _generate_jwt(private_key, kid, project_id, now)
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
