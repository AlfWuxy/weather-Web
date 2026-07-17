#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证候选发布配置的完整性，输出不含密钥的 readiness 摘要。"""

from __future__ import annotations

import argparse
import json
import re
import stat
from datetime import date
from pathlib import Path
from urllib.parse import urlparse


WECHAT_APP_KEYS = (
    "WX_MINIPROGRAM_APPID",
    "WX_MINIPROGRAM_SECRET",
)
WECHAT_SERVER_KEYS = (
    "WX_MINIPROGRAM_OPENID_PEPPER",
    "WX_MINIPROGRAM_SESSION_SECRET",
)
WECHAT_KEYS = WECHAT_APP_KEYS + WECHAT_SERVER_KEYS
WECHAT_FORM_REQUIRED_KEYS = (
    "WECHAT_MINIPROGRAM_NAME",
    "WECHAT_OPERATOR_NAME",
    "WECHAT_CONTACT_EMAIL",
    "WECHAT_EFFECTIVE_DATE",
    "WX_MINIPROGRAM_APPID",
    "WX_MINIPROGRAM_SECRET",
    "WX_MINIPROGRAM_PRIVACY_VERSION",
)
QWEATHER_MAX_PRIVATE_KEY_BYTES = 16 * 1024
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APPID_PATTERN = re.compile(r"^wx[A-Za-z0-9]{6,32}$")


def _read_env(path: Path):
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def validate_wechat_release_form(path: Path, *, require_ready=False):
    """校验本机私密发布表单，只返回状态与错误，不返回任何填写值。"""
    errors = []
    warnings = []
    if not path.exists():
        message = "微信发布私密表单不存在，请复制 .env.wechat-release.example 后填写。"
        (errors if require_ready else warnings).append(message)
        return {
            "ok": not errors,
            "form_ready": False,
            "category_confirmed": False,
            "warnings": warnings,
            "errors": errors,
        }

    try:
        file_stat = path.lstat()
    except OSError:
        errors.append("微信发布私密表单无法读取。")
        return {
            "ok": False,
            "form_ready": False,
            "category_confirmed": False,
            "warnings": warnings,
            "errors": errors,
        }
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        errors.append("微信发布私密表单必须是普通文件，不能使用符号链接。")
    if file_stat.st_mode & 0o077:
        errors.append("微信发布私密表单权限必须为 0600，请先执行 chmod 600。")

    values = _read_env(path)
    form_ready = values.get("WECHAT_FORM_READY") == "1"
    category_confirmed = values.get("WECHAT_CATEGORY_CONFIRMED") == "1"
    if values.get("WECHAT_SUBJECT_TYPE") != "personal":
        errors.append("WECHAT_SUBJECT_TYPE 必须保持 personal。")
    if values.get("WECHAT_FORM_READY", "0") not in {"0", "1"}:
        errors.append("WECHAT_FORM_READY 只能是 0 或 1。")
    if values.get("WECHAT_CATEGORY_CONFIRMED", "0") not in {"0", "1"}:
        errors.append("WECHAT_CATEGORY_CONFIRMED 只能是 0 或 1。")

    must_be_complete = require_ready or form_ready
    if require_ready and not form_ready:
        errors.append("正式发布前必须将 WECHAT_FORM_READY 设为 1。")
    if must_be_complete and not category_confirmed:
        errors.append("正式发布前必须在后台确认个人主体可用类目，并设置 WECHAT_CATEGORY_CONFIRMED=1。")
    if must_be_complete:
        missing = [key for key in WECHAT_FORM_REQUIRED_KEYS if not values.get(key)]
        if missing:
            errors.append("微信发布私密表单缺少必填字段: " + ", ".join(missing))

        appid = values.get("WX_MINIPROGRAM_APPID", "")
        secret = values.get("WX_MINIPROGRAM_SECRET", "")
        if appid and not APPID_PATTERN.fullmatch(appid):
            errors.append("WX_MINIPROGRAM_APPID 格式异常。")
        if secret and len(secret) < 16:
            errors.append("WX_MINIPROGRAM_SECRET 长度异常。")
        contact_email = values.get("WECHAT_CONTACT_EMAIL", "")
        if contact_email and not EMAIL_PATTERN.fullmatch(contact_email):
            errors.append("WECHAT_CONTACT_EMAIL 格式异常。")
        effective_date = values.get("WECHAT_EFFECTIVE_DATE", "")
        if effective_date:
            try:
                date.fromisoformat(effective_date)
            except ValueError:
                errors.append("WECHAT_EFFECTIVE_DATE 必须使用 YYYY-MM-DD。")
        if len(values.get("WECHAT_OPERATOR_NAME", "")) > 80:
            errors.append("WECHAT_OPERATOR_NAME 长度异常。")

    if not form_ready and not require_ready:
        warnings.append("微信发布私密表单尚未完成，当前只能进行游客模式预览。")
    return {
        "ok": not errors,
        "form_ready": form_ready and not errors,
        "category_confirmed": category_confirmed,
        "warnings": warnings,
        "errors": errors,
    }


def _validate_qweather_base(api_base: str, *, auth_mode: str):
    errors = []
    parsed = urlparse(api_base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        errors.append("QWEATHER_API_BASE 必须是无用户信息、查询参数和片段的 HTTPS URL。")
        return errors
    if auth_mode == "jwt":
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname != "qweatherapi.com" and not hostname.endswith(".qweatherapi.com"):
            errors.append("QWeather JWT Host 必须是 qweatherapi.com 或其子域名。")
        if parsed.path.rstrip("/") != "/v7":
            errors.append("QWeather JWT API Base 路径必须为 /v7。")
    return errors


def _validate_qweather_private_key(path_value: str):
    errors = []
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        return ["QWEATHER_JWT_PRIVATE_KEY_PATH 必须是服务器上的绝对路径。"]
    try:
        file_stat = path.stat()
    except OSError:
        return ["QWeather JWT 私钥文件不存在或不可读取。"]
    if not stat.S_ISREG(file_stat.st_mode):
        errors.append("QWeather JWT 私钥必须是普通文件。")
    if file_stat.st_mode & 0o077:
        errors.append("QWeather JWT 私钥权限必须为 0600 或更严格。")
    if file_stat.st_size <= 0 or file_stat.st_size > QWEATHER_MAX_PRIVATE_KEY_BYTES:
        errors.append("QWeather JWT 私钥文件大小异常。")
    return errors


def validate_release_env(path: Path, *, require_wechat=False):
    values = _read_env(path)
    errors = []
    warnings = []

    public_base_url = values.get("PUBLIC_BASE_URL", "")
    parsed_public = urlparse(public_base_url)
    insecure_allowed = values.get("ALLOW_INSECURE_PUBLIC_BASE_URL") == "1"
    if not public_base_url:
        errors.append("PUBLIC_BASE_URL 未配置，生产推送链接需要 HTTPS 域名。")
    elif parsed_public.scheme == "https" and parsed_public.netloc:
        pass
    elif parsed_public.scheme == "http" and parsed_public.netloc and insecure_allowed:
        warnings.append("当前显式允许 HTTP PUBLIC_BASE_URL，仅适合临时验收。")
    else:
        errors.append("PUBLIC_BASE_URL 必须使用 HTTPS，或显式临时允许 HTTP。")

    wechat_app_present = [key for key in WECHAT_APP_KEYS if values.get(key)]
    wechat_server_present = [key for key in WECHAT_SERVER_KEYS if values.get(key)]
    if wechat_app_present and len(wechat_app_present) != len(WECHAT_APP_KEYS):
        errors.append("WX_MINIPROGRAM_APPID 与 WX_MINIPROGRAM_SECRET 必须同时填写。")
    if wechat_server_present and len(wechat_server_present) != len(WECHAT_SERVER_KEYS):
        errors.append("微信身份 pepper 与会话密钥必须同时配置。")

    wechat_ready = (
        len(wechat_app_present) == len(WECHAT_APP_KEYS)
        and len(wechat_server_present) == len(WECHAT_SERVER_KEYS)
    )
    if wechat_app_present and not wechat_ready:
        errors.append("微信登录凭证存在时，四项服务端配置必须完整。")
    elif not wechat_ready:
        message = "微信登录配置待认证后填写，当前仅可运行 Web/公开预览能力。"
        (errors if require_wechat else warnings).append(message)
    if wechat_ready:
        if len(values["WX_MINIPROGRAM_APPID"]) < 6:
            errors.append("WX_MINIPROGRAM_APPID 长度异常。")
        if len(values["WX_MINIPROGRAM_SECRET"]) < 16:
            errors.append("WX_MINIPROGRAM_SECRET 长度异常。")
    for key in WECHAT_SERVER_KEYS:
        if values.get(key) and len(values[key]) < 32:
            errors.append(f"{key} 必须至少 32 位。")

    qweather_mode = values.get("QWEATHER_AUTH_MODE", "disabled").lower()
    qweather_base = values.get("QWEATHER_API_BASE", "")
    allow_weather_unavailable = values.get("ALLOW_WEATHER_UNAVAILABLE") == "1"
    weather_ready = False
    if qweather_mode == "disabled":
        message = "和风天气同步当前停用，新服务器没有可用天气快照。"
        can_run_degraded_preview = allow_weather_unavailable and not require_wechat
        (warnings if can_run_degraded_preview else errors).append(message)
    elif qweather_mode == "api_key":
        if not values.get("QWEATHER_KEY") or not qweather_base:
            errors.append("QWEATHER_AUTH_MODE=api_key 时必须同时配置 Key 与 API Base。")
        else:
            mode_errors = _validate_qweather_base(qweather_base, auth_mode=qweather_mode)
            errors.extend(mode_errors)
            weather_ready = not mode_errors
    elif qweather_mode == "jwt":
        required = (
            "QWEATHER_JWT_KID",
            "QWEATHER_JWT_PROJECT_ID",
            "QWEATHER_JWT_PRIVATE_KEY_PATH",
        )
        if not qweather_base or any(not values.get(key) for key in required):
            errors.append("QWEATHER_AUTH_MODE=jwt 时必须完整配置 API Base 与三项 JWT 参数。")
        else:
            mode_errors = _validate_qweather_base(qweather_base, auth_mode=qweather_mode)
            mode_errors.extend(
                _validate_qweather_private_key(values["QWEATHER_JWT_PRIVATE_KEY_PATH"])
            )
            errors.extend(mode_errors)
            weather_ready = not mode_errors
    else:
        errors.append("QWEATHER_AUTH_MODE 只能是 disabled、api_key 或 jwt。")
    return {
        "ok": not errors,
        "wechat_ready": wechat_ready,
        "weather_ready": weather_ready,
        "qweather_mode": qweather_mode,
        "wxpusher_ready": bool(values.get("WXPUSHER_APP_TOKEN")),
        "warnings": warnings,
        "errors": errors,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate staged release environment.")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--wechat-form", type=Path)
    parser.add_argument("--form-only", action="store_true")
    parser.add_argument("--require-wechat", choices=("0", "1"), default="0")
    args = parser.parse_args(argv)
    require_wechat = args.require_wechat == "1"
    if args.form_only:
        if not args.wechat_form:
            parser.error("--form-only 必须同时提供 --wechat-form")
        result = validate_wechat_release_form(
            args.wechat_form,
            require_ready=require_wechat,
        )
    else:
        if not args.file:
            parser.error("必须提供 --file")
        result = validate_release_env(args.file, require_wechat=require_wechat)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
