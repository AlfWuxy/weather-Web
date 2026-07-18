#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证候选发布配置的完整性，输出不含密钥的 readiness 摘要。"""

from __future__ import annotations

import argparse
import errno
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


WECHAT_APP_KEYS = (
    "WX_MINIPROGRAM_APPID",
    "WX_MINIPROGRAM_SECRET",
)
WECHAT_SERVER_KEYS = (
    "WX_MINIPROGRAM_OPENID_PEPPER",
    "WX_MINIPROGRAM_SESSION_SECRET",
)
WECHAT_KEYS = WECHAT_APP_KEYS + WECHAT_SERVER_KEYS
WECHAT_CATEGORY_EVIDENCE_KEYS = (
    "WECHAT_CATEGORY_PATHS_JSON",
    "WECHAT_CATEGORY_QUALIFICATION_STATUS",
    "WECHAT_CATEGORY_EVIDENCE_ROOT",
    "WECHAT_CATEGORY_EVIDENCE_REF",
    "WECHAT_CATEGORY_EVIDENCE_SHA256",
    "WECHAT_CATEGORY_CONFIRMED_AT",
)
WECHAT_RELEASE_SHA256_KEYS = (
    "WECHAT_PRIVACY_DOC_SHA256",
    "WECHAT_AGREEMENT_DOC_SHA256",
    "WECHAT_LISTING_COPY_SHA256",
    "WECHAT_PRIVACY_PAGE_SHA256",
    "WECHAT_AGREEMENT_PAGE_SHA256",
)
WECHAT_RELEASE_ARTIFACTS = (
    ("WECHAT_PRIVACY_DOC_SHA256", "docs/miniprogram/PRIVACY_NOTICE_TEMPLATE.md"),
    ("WECHAT_AGREEMENT_DOC_SHA256", "docs/miniprogram/USER_AGREEMENT_TEMPLATE.md"),
    ("WECHAT_LISTING_COPY_SHA256", "docs/miniprogram/LISTING_COPY.md"),
    ("WECHAT_PRIVACY_PAGE_SHA256", "miniprogram/pages/privacy/index.wxml"),
    ("WECHAT_AGREEMENT_PAGE_SHA256", "miniprogram/pages/agreement/index.wxml"),
)
WECHAT_RELEASE_CANDIDATE_MARKER = "候选"
WECHAT_RELEASE_FINAL_STATUS = "final"
WECHAT_RELEASE_FINAL_STATUS_MARKER = "<!-- WECHAT_RELEASE_STATUS: final -->"
WECHAT_MINIPROGRAM_NAME_MARKER_FORMAT = (
    "<!-- WECHAT_MINIPROGRAM_NAME: {value} -->"
)
WECHAT_EFFECTIVE_DATE_MARKER_FORMAT = "<!-- WECHAT_EFFECTIVE_DATE: {value} -->"
WECHAT_PRIVACY_VERSION_MARKER_FORMAT = "<!-- WECHAT_PRIVACY_VERSION: {value} -->"
WECHAT_EFFECTIVE_DATE_ARTIFACT_KEYS = {
    "WECHAT_PRIVACY_DOC_SHA256",
    "WECHAT_AGREEMENT_DOC_SHA256",
    "WECHAT_PRIVACY_PAGE_SHA256",
    "WECHAT_AGREEMENT_PAGE_SHA256",
}
WECHAT_PRIVACY_VERSION_ARTIFACT_KEYS = {
    "WECHAT_PRIVACY_DOC_SHA256",
    "WECHAT_PRIVACY_PAGE_SHA256",
}
WECHAT_PROJECT_CONFIG_PATH = "project.config.json"
WECHAT_PROJECT_PRIVATE_CONFIG_PATH = "project.private.config.json"
WECHAT_PROJECT_CONFIG_PLACEHOLDER_APPID = "touristappid"
WECHAT_PROJECT_PRIVATE_CONFIG_MAX_BYTES = 64 * 1024
WECHAT_PROJECT_PRIVATE_FORBIDDEN_KEYS = frozenset(
    {
        "appsecret",
        "miniprogramappsecret",
        "miniprogramsecret",
        "wechatappsecret",
        "wxminiprogramsecret",
    }
)
WECHAT_MINIPROGRAM_CONFIG_PATH = "miniprogram/config.js"
WECHAT_MINIPROGRAM_RUNTIME_CONFIG_PATH = "miniprogram/config.runtime.js"
WECHAT_RELEASE_FREEZE_KEYS = (
    "WECHAT_RELEASE_VERSION",
    "WECHAT_TARGET_COMMIT_SHA",
) + WECHAT_RELEASE_SHA256_KEYS
WECHAT_FORM_REQUIRED_KEYS = (
    "WECHAT_MINIPROGRAM_NAME",
    "WECHAT_OPERATOR_NAME",
    "WECHAT_CONTACT_EMAIL",
    "WECHAT_EFFECTIVE_DATE",
    "WECHAT_REQUEST_DOMAIN",
    "WX_MINIPROGRAM_APPID",
    "WX_MINIPROGRAM_SECRET",
    "WX_MINIPROGRAM_PRIVACY_VERSION",
    "FEATURE_WXPUSHER",
    "FEATURE_HEAT_EXPOSURE_GIS",
    "QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED",
    "QWEATHER_CONSOLE_USAGE_MONTH",
    "QWEATHER_CONSOLE_USAGE_BASELINE",
) + WECHAT_CATEGORY_EVIDENCE_KEYS + WECHAT_RELEASE_FREEZE_KEYS
QWEATHER_MAX_PRIVATE_KEY_BYTES = 16 * 1024
GIS_SOURCE_MAX_BYTES = 8 * 1024 * 1024
GIS_COMPRESSED_MAX_BYTES = 300 * 1024
GIS_FROZEN_ARTIFACT_PATH = "static/data/gis/duchang_heat_exposure_cells.geojson"
QWEATHER_FORMAL_PINNED_VALUES = {
    "QWEATHER_CANONICAL_LOCATION": "116.20,29.27",
    "QWEATHER_MONTHLY_REQUEST_LIMIT": "40000",
    "QWEATHER_BUDGET_FAIL_CLOSED": "1",
    "QWEATHER_REQUIRE_PERSISTENT_BUDGET": "1",
    "WEATHER_CACHE_TTL_MINUTES": "30",
    "FORECAST_CACHE_TTL_MINUTES": "30",
    "QWEATHER_WARNING_CACHE_TTL_MINUTES": "30",
    "WEATHER_SYNC_LOCATIONS": "都昌县",
}
QWEATHER_CONSOLE_USAGE_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
QWEATHER_CONSOLE_USAGE_BASELINE_PATTERN = re.compile(r"^(?:0|[1-9]\d{0,8})$")
QWEATHER_REDIS_BUDGET_PREFIX = "qweather:budget:app:v2"
QWEATHER_FORMAL_SMOKE_MAX_REQUESTS = 3
WECHAT_CATEGORY_PATHS_JSON_MAX_LENGTH = 1800
WECHAT_CATEGORY_PATH_MAX_LENGTH = 200
WECHAT_CATEGORY_SEGMENT_MAX_LENGTH = 80
WECHAT_CATEGORY_MAX_PATHS = 8
WECHAT_CATEGORY_EVIDENCE_REF_MAX_LENGTH = 240
WECHAT_CATEGORY_CONFIRMED_AT_MAX_LENGTH = 40
WECHAT_CATEGORY_EVIDENCE_MAX_AGE = timedelta(hours=24)
WECHAT_RELEASE_FORM_MAX_BYTES = 64 * 1024
WECHAT_CATEGORY_EVIDENCE_MAX_BYTES = 20 * 1024 * 1024
WECHAT_CATEGORY_NO_EXTRA_QUALIFICATION = "no_extra_institutional_qualification"
WECHAT_EXPECTED_RELEASE_VERSION = "1.0.0"
WECHAT_EXPECTED_REQUEST_DOMAIN = "https://yilaoweather.org"
WXPUSHER_EXPECTED_API_BASE = "https://wxpusher.zjiecode.com/api"
EXPECTED_REQUIREMENTS_LOCK_SHA256 = (
    "c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853"
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APPID_PATTERN = re.compile(r"^wx[A-Za-z0-9]{6,32}$")
WXPUSHER_APP_TOKEN_PATTERN = re.compile(r"^AT_[A-Za-z0-9_-]{16,197}$")
SEMVER_PATTERN = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
LOWER_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
LOWER_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WECHAT_CATEGORY_EVIDENCE_REF_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
)
WECHAT_CATEGORY_CONFIRMED_AT_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
WECHAT_RELEASE_STATUS_MARKER_PATTERN = re.compile(
    r"^<!-- WECHAT_RELEASE_STATUS: ([a-z]+) -->$",
    re.MULTILINE,
)
WECHAT_MINIPROGRAM_NAME_MARKER_PATTERN = re.compile(
    r"^<!-- WECHAT_MINIPROGRAM_NAME: ([^<>\r\n]+) -->$",
    re.MULTILINE,
)
WECHAT_EFFECTIVE_DATE_MARKER_PATTERN = re.compile(
    r"^<!-- WECHAT_EFFECTIVE_DATE: (\d{4}-\d{2}-\d{2}) -->$",
    re.MULTILINE,
)
WECHAT_PRIVACY_VERSION_MARKER_PATTERN = re.compile(
    r"^<!-- WECHAT_PRIVACY_VERSION: ([A-Za-z0-9._-]+) -->$",
    re.MULTILINE,
)
WECHAT_VISIBLE_EFFECTIVE_DATE_PATTERN = re.compile(r"生效日期：(\d{4}-\d{2}-\d{2})")
WECHAT_VISIBLE_PRIVACY_VERSION_PATTERN = re.compile(
    r"隐私版本：([A-Za-z0-9._-]+)"
)
PRIVACY_CONSENT_VERSION_PATTERN = re.compile(
    r"^\s*PRIVACY_CONSENT_VERSION\s*:\s*(['\"])([^'\"\r\n]+)\1\s*,?\s*$",
    re.MULTILINE,
)
API_BASE_URL_PATTERN = re.compile(
    r"^\s*API_BASE_URL\s*:\s*(['\"])([^'\"\r\n]*)\1\s*,?\s*$",
    re.MULTILINE,
)
QWEATHER_API_AUTHORITY_PATTERN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"qweatherapi\.com(?::443)?$",
    re.IGNORECASE,
)
HTML_COMMENT_PATTERN = re.compile(r"<!--[\s\S]*?-->")
SCRIPT_STYLE_PATTERN = re.compile(
    r"<(script|style)\b[^>]*>[\s\S]*?</\1\s*>",
    re.IGNORECASE,
)


def _parse_env(content: bytes):
    text = content.decode("utf-8")
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _file_fingerprint(file_stat):
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _read_open_file_stably(
    file_descriptor: int,
    *,
    max_bytes: int,
    require_private: bool,
    require_nonempty: bool = False,
    required_mode: int | None = None,
):
    """从已安全打开的普通文件读取固定快照，并检测读中变化和增长。"""
    try:
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None, "type"
        if required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode:
            return None, "permission"
        if required_mode is None and require_private and before.st_mode & 0o077:
            return None, "permission"
        if before.st_size > max_bytes or (require_nonempty and before.st_size <= 0):
            return None, "size"

        chunks = []
        total = 0
        while True:
            chunk = os.read(file_descriptor, min(8192, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                return None, "size"
        after = os.fstat(file_descriptor)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EISDIR}:
            return None, "type"
        return None, "io"

    if total != before.st_size or _file_fingerprint(before) != _file_fingerprint(after):
        return None, "changed"
    if require_nonempty and total == 0:
        return None, "size"
    return b"".join(chunks), None


def _read_regular_file_stably(
    path: Path,
    *,
    max_bytes: int,
    require_private: bool,
    require_nonempty: bool = False,
    required_mode: int | None = None,
):
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return None, "io"
    try:
        file_descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return None, "missing"
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EISDIR}:
            return None, "type"
        return None, "io"
    try:
        return _read_open_file_stably(
            file_descriptor,
            max_bytes=max_bytes,
            require_private=require_private,
            require_nonempty=require_nonempty,
            required_mode=required_mode,
        )
    finally:
        os.close(file_descriptor)


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _expected_qweather_usage_month(validation_time: datetime | None = None):
    now = validation_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")


def _qweather_required_month_reserve(validation_time: datetime | None = None):
    """预留本次烟测和直到北京时间下月起点的 30 分钟正式同步。"""
    now = validation_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=now.tzinfo)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=now.tzinfo)
    remaining_seconds = max((next_month - now).total_seconds(), 0)
    scheduled_cycles = math.ceil(remaining_seconds / 1800)
    return (
        scheduled_cycles * QWEATHER_FORMAL_SMOKE_MAX_REQUESTS
        + QWEATHER_FORMAL_SMOKE_MAX_REQUESTS
    )


def _validate_qweather_console_baseline(values, *, validation_time=None):
    errors = []
    if values.get("QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED", "") != "1":
        errors.append("正式发布必须确认 QWeather 凭据仅供本项目使用。")
    usage_month = values.get("QWEATHER_CONSOLE_USAGE_MONTH", "")
    if not QWEATHER_CONSOLE_USAGE_MONTH_PATTERN.fullmatch(usage_month):
        errors.append("QWEATHER_CONSOLE_USAGE_MONTH 必须使用 YYYY-MM。")
    elif usage_month != _expected_qweather_usage_month(validation_time):
        errors.append("QWEATHER_CONSOLE_USAGE_MONTH 必须是当前北京时间月份。")
    baseline = values.get("QWEATHER_CONSOLE_USAGE_BASELINE", "")
    if not QWEATHER_CONSOLE_USAGE_BASELINE_PATTERN.fullmatch(baseline):
        errors.append("QWEATHER_CONSOLE_USAGE_BASELINE 必须是 0 至 999999999 的整数。")
    elif int(baseline) + _qweather_required_month_reserve(validation_time) > 40000:
        errors.append(
            "QWeather 控制台当月剩余额度不足以覆盖最多 3 个正式烟测端点，"
            "以及每 30 分钟最多 3 个端点直至北京时间下月起点。"
        )
    return errors


def _validate_wechat_category_paths_json(value: str):
    """校验后台完整类目路径列表，不在错误信息中回显路径内容。"""
    errors = []
    if len(value) > WECHAT_CATEGORY_PATHS_JSON_MAX_LENGTH:
        return ["WECHAT_CATEGORY_PATHS_JSON 总长度异常。"]
    try:
        category_paths = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ["WECHAT_CATEGORY_PATHS_JSON 必须是 JSON 字符串数组。"]
    if (
        not isinstance(category_paths, list)
        or not category_paths
        or len(category_paths) > WECHAT_CATEGORY_MAX_PATHS
    ):
        return ["WECHAT_CATEGORY_PATHS_JSON 必须包含 1 至 8 条完整类目路径。"]

    for category_path in category_paths:
        if not isinstance(category_path, str):
            errors.append("WECHAT_CATEGORY_PATHS_JSON 的每一项都必须是字符串。")
            break
        if (
            not category_path
            or category_path != category_path.strip()
            or len(category_path) > WECHAT_CATEGORY_PATH_MAX_LENGTH
        ):
            errors.append("WECHAT_CATEGORY_PATHS_JSON 中的类目路径长度异常。")
            break
        if (
            _contains_control_character(category_path)
            or "\\" in category_path
            or "://" in category_path
        ):
            errors.append("WECHAT_CATEGORY_PATHS_JSON 中的类目路径包含不安全字符。")
            break
        segments = category_path.split("/")
        if (
            len(segments) < 2
            or any(not segment.strip() for segment in segments)
            or any(segment.strip() in {".", ".."} for segment in segments)
            or any(
                len(segment.strip()) > WECHAT_CATEGORY_SEGMENT_MAX_LENGTH
                for segment in segments
            )
        ):
            errors.append("WECHAT_CATEGORY_PATHS_JSON 必须记录完整且无路径跳转的类目路径。")
            break
    return errors


def _validate_wechat_category_evidence_ref(value: str):
    """证据只保存私有归档内的相对引用，拒绝绝对路径和目录跳转。"""
    if (
        not value
        or len(value) > WECHAT_CATEGORY_EVIDENCE_REF_MAX_LENGTH
        or not WECHAT_CATEGORY_EVIDENCE_REF_PATTERN.fullmatch(value)
    ):
        return ["WECHAT_CATEGORY_EVIDENCE_REF 证据引用格式或长度异常。"]
    parts = value.split("/")
    if value.endswith("/") or any(part in {"", ".", ".."} for part in parts):
        return ["WECHAT_CATEGORY_EVIDENCE_REF 证据引用禁止绝对路径或目录跳转。"]
    return []


def _validate_wechat_category_evidence_file(values, repo_root: Path):
    """验证仓库外私有证据文件及摘要，不回显本机路径或文件内容。"""
    errors = []
    root_value = values.get("WECHAT_CATEGORY_EVIDENCE_ROOT", "")
    ref_value = values.get("WECHAT_CATEGORY_EVIDENCE_REF", "")
    expected_digest = values.get("WECHAT_CATEGORY_EVIDENCE_SHA256", "")
    root = Path(root_value)
    if not root.is_absolute():
        return ["WECHAT_CATEGORY_EVIDENCE_ROOT 必须是仓库外的私有绝对目录。"]
    try:
        root_resolved = root.resolve(strict=True)
        repo_resolved = repo_root.resolve(strict=True)
        candidate_resolved = (root / ref_value).resolve(strict=True)
        candidate_resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError):
        return ["WECHAT_CATEGORY_EVIDENCE_ROOT 或证据文件无法安全验证。"]
    if root_resolved == repo_resolved or repo_resolved in root_resolved.parents:
        return ["WECHAT_CATEGORY_EVIDENCE_ROOT 必须位于 Git 仓库之外。"]
    if candidate_resolved == repo_resolved or repo_resolved in candidate_resolved.parents:
        return ["WECHAT_CATEGORY_EVIDENCE_REF 对应证据必须位于 Git 仓库之外。"]

    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None:
        return ["当前系统无法安全验证类目证据文件。"]

    opened_directories = []
    evidence_fd = None
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | directory_flag | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        opened_directories.append(root_fd)
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_mode & 0o077:
            return ["WECHAT_CATEGORY_EVIDENCE_ROOT 必须是仅当前用户可访问的目录。"]

        current_fd = root_fd
        parts = ref_value.split("/")
        for directory_name in parts[:-1]:
            next_fd = os.open(
                directory_name,
                os.O_RDONLY
                | directory_flag
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current_fd,
            )
            opened_directories.append(next_fd)
            directory_stat = os.fstat(next_fd)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_mode & 0o077
            ):
                return ["WECHAT_CATEGORY_EVIDENCE_REF 的中间目录权限不安全。"]
            current_fd = next_fd

        evidence_fd = os.open(
            parts[-1],
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=current_fd,
        )
        content, read_error = _read_open_file_stably(
            evidence_fd,
            max_bytes=WECHAT_CATEGORY_EVIDENCE_MAX_BYTES,
            require_private=True,
            require_nonempty=True,
        )
    except OSError:
        return ["WECHAT_CATEGORY_EVIDENCE_REF 对应证据文件无法安全读取。"]
    finally:
        if evidence_fd is not None:
            os.close(evidence_fd)
        for directory_fd in reversed(opened_directories):
            os.close(directory_fd)

    if read_error == "type":
        errors.append("WECHAT_CATEGORY_EVIDENCE_REF 对应证据必须是普通文件。")
    elif read_error == "permission":
        errors.append("WECHAT_CATEGORY_EVIDENCE_REF 对应证据文件权限必须为 0600。")
    elif read_error == "size":
        errors.append("WECHAT_CATEGORY_EVIDENCE_REF 对应证据文件大小异常。")
    elif read_error == "changed":
        errors.append("WECHAT_CATEGORY_EVIDENCE_REF 对应证据文件读取期间发生变化。")
    elif read_error is not None:
        errors.append("WECHAT_CATEGORY_EVIDENCE_REF 对应证据文件无法安全读取。")
    elif hashlib.sha256(content).hexdigest() != expected_digest:
        errors.append("WECHAT_CATEGORY_EVIDENCE_SHA256 与类目证据文件不一致。")
    return errors


def _validate_wechat_category_confirmed_at(value: str):
    """确认时间必须带显式时区，避免发布证据跨时区失去确定性。"""
    if (
        len(value) > WECHAT_CATEGORY_CONFIRMED_AT_MAX_LENGTH
        or not WECHAT_CATEGORY_CONFIRMED_AT_PATTERN.fullmatch(value)
    ):
        return ["WECHAT_CATEGORY_CONFIRMED_AT 必须是带时区的 ISO 8601 时间。"]
    try:
        confirmed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
            raise ValueError
    except ValueError:
        return ["WECHAT_CATEGORY_CONFIRMED_AT 必须是有效且带时区的 ISO 8601 时间。"]
    return []


def _validate_wechat_category_evidence_freshness(
    value: str,
    *,
    validation_time: datetime | None = None,
):
    """正式发布证据按 UTC 比较，未来时间和超过 24 小时的截图均失效。"""
    try:
        confirmed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        current_time = validation_time or datetime.now(timezone.utc)
        if (
            confirmed_at.tzinfo is None
            or confirmed_at.utcoffset() is None
            or current_time.tzinfo is None
            or current_time.utcoffset() is None
        ):
            raise ValueError
    except (TypeError, ValueError):
        return ["WECHAT_CATEGORY_CONFIRMED_AT 无法用于正式发布时间校验。"]

    evidence_time_utc = confirmed_at.astimezone(timezone.utc)
    validation_time_utc = current_time.astimezone(timezone.utc)
    evidence_age = validation_time_utc - evidence_time_utc
    if evidence_age < timedelta(0):
        return ["WECHAT_CATEGORY_CONFIRMED_AT 不能晚于当前校验时间。"]
    if evidence_age > WECHAT_CATEGORY_EVIDENCE_MAX_AGE:
        return ["WECHAT_CATEGORY_CONFIRMED_AT 已超过 24 小时，必须重新取证。"]
    return []


def _validate_wechat_request_domain(value: str):
    """正式 request 合法域名必须是无附加组件的固定 HTTPS origin。"""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return ["WECHAT_REQUEST_DOMAIN 必须是合法的 HTTPS origin。"]
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        return ["WECHAT_REQUEST_DOMAIN 必须是无路径、查询或片段的 HTTPS origin。"]
    if value != WECHAT_EXPECTED_REQUEST_DOMAIN:
        return ["WECHAT_REQUEST_DOMAIN 必须保持正式 request 合法域名。"]
    return []


def _visible_release_body(value: str) -> str:
    """移除不会呈现给审核人员的注释和脚本样式内容。"""
    without_script_style = SCRIPT_STYLE_PATTERN.sub("", value)
    return HTML_COMMENT_PATTERN.sub("", without_script_style)


def _validate_deploy_dependency_lock() -> list[str]:
    """固定部署依赖闭包，并确认 WSGI 运行器包含在哈希锁中。"""
    lock_path = Path(__file__).resolve().parents[1] / "requirements.lock"
    try:
        content = lock_path.read_bytes()
    except OSError:
        return ["部署依赖锁无法读取。"]
    if hashlib.sha256(content).hexdigest() != EXPECTED_REQUIREMENTS_LOCK_SHA256:
        return ["requirements.lock 摘要与正式发布基线不一致。"]
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ["requirements.lock 必须是 UTF-8 文本。"]
    if not re.search(r"^gunicorn==[^\s\\]+\s*\\$", text, re.MULTILINE):
        return ["requirements.lock 缺少固定 gunicorn 依赖。"]
    return []


def _validate_wechat_release_freeze(values):
    """校验最终发布标识与材料摘要，仅返回字段级错误。"""
    errors = []
    release_version = values.get("WECHAT_RELEASE_VERSION", "")
    if release_version:
        if not SEMVER_PATTERN.fullmatch(release_version):
            errors.append("WECHAT_RELEASE_VERSION 必须是合法的 SemVer 版本号。")
        elif release_version != WECHAT_EXPECTED_RELEASE_VERSION:
            errors.append("WECHAT_RELEASE_VERSION 当前正式首发版本必须为 1.0.0。")

    target_commit = values.get("WECHAT_TARGET_COMMIT_SHA", "")
    if target_commit and not LOWER_COMMIT_SHA_PATTERN.fullmatch(target_commit):
        errors.append("WECHAT_TARGET_COMMIT_SHA 必须是 40 位小写十六进制。")

    for key in WECHAT_RELEASE_SHA256_KEYS:
        digest = values.get(key, "")
        if digest and not LOWER_SHA256_PATTERN.fullmatch(digest):
            errors.append(f"{key} 必须是 64 位小写十六进制 SHA-256。")
    return errors


def _run_git(repo_root: Path, *args: str):
    """执行只读 Git 命令，失败时不向上层暴露命令输出或本机路径。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _private_project_config_contains_appsecret(value):
    """递归识别常见 AppSecret 字段名，不检查或回显字段值。"""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if (
                normalized_key in WECHAT_PROJECT_PRIVATE_FORBIDDEN_KEYS
                or normalized_key.endswith("appsecret")
            ):
                return True
            if _private_project_config_contains_appsecret(child):
                return True
    elif isinstance(value, list):
        return any(_private_project_config_contains_appsecret(item) for item in value)
    return False


def _validate_wechat_private_project_config(values, repo_root: Path):
    """安全核对仅保存在本机且被 Git 忽略的小程序工程配置。"""
    errors = []
    ignored = _run_git(
        repo_root,
        "check-ignore",
        "--quiet",
        "--",
        WECHAT_PROJECT_PRIVATE_CONFIG_PATH,
    )
    if ignored is None:
        errors.append("本机小程序私有工程配置必须被 Git 忽略。")

    content, read_error = _read_regular_file_stably(
        repo_root / WECHAT_PROJECT_PRIVATE_CONFIG_PATH,
        max_bytes=WECHAT_PROJECT_PRIVATE_CONFIG_MAX_BYTES,
        require_private=True,
        require_nonempty=True,
        required_mode=0o600,
    )
    read_error_messages = {
        "missing": "本机小程序私有工程配置不存在或不可读取。",
        "type": "本机小程序私有工程配置必须是普通文件且不能是符号链接。",
        "permission": "本机小程序私有工程配置权限必须严格为 0600。",
        "size": "本机小程序私有工程配置大小异常。",
        "changed": "本机小程序私有工程配置读取期间发生变化，请重新执行。",
        "io": "本机小程序私有工程配置无法安全读取。",
    }
    if read_error is not None:
        errors.append(
            read_error_messages.get(
                read_error,
                "本机小程序私有工程配置无法安全读取。",
            )
        )
        return errors

    try:
        private_values = json.loads(content)
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        errors.append("本机小程序私有工程配置必须是有效 JSON 对象。")
        return errors
    if not isinstance(private_values, dict):
        errors.append("本机小程序私有工程配置必须是有效 JSON 对象。")
        return errors

    if _private_project_config_contains_appsecret(private_values):
        errors.append("本机小程序私有工程配置不得包含 AppSecret 字段。")

    private_appid = private_values.get("appid")
    if not isinstance(private_appid, str) or not private_appid:
        errors.append("本机小程序私有工程配置的 AppID 字段无效。")
    elif private_appid != values.get("WX_MINIPROGRAM_APPID", ""):
        errors.append("WX_MINIPROGRAM_APPID 与本机小程序私有工程配置不一致。")
    return errors


def _brotli_compress_with_node(content: bytes, *, node_bin: str | None = None):
    """使用 Node 标准库生成 Brotli 冻结体，不依赖额外 Python 包。"""
    executable = node_bin or shutil.which("node")
    if not executable:
        return None
    program = (
        "const z=require('zlib');const c=[];"
        "process.stdin.on('data',x=>c.push(x));"
        "process.stdin.on('end',()=>{z.brotliCompress(Buffer.concat(c),"
        "{params:{[z.constants.BROTLI_PARAM_QUALITY]:11}},(e,o)=>{"
        "if(e)process.exit(2);process.stdout.write(o);});});"
    )
    try:
        result = subprocess.run(
            [executable, "-e", program],
            input=content,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _validate_gis_compressed_content(content: bytes, *, node_bin: str | None = None):
    """校验正式 GIS 在两种常用传输压缩下均低于 300 KiB。"""
    if not content or len(content) > GIS_SOURCE_MAX_BYTES:
        return ["冻结 GIS 源文件缺失或大小异常。"]
    try:
        gzip_content = gzip.compress(content, compresslevel=9, mtime=0)
    except (OSError, ValueError):
        return ["冻结 GIS gzip 压缩门禁无法完成。"]
    brotli_content = _brotli_compress_with_node(content, node_bin=node_bin)
    if brotli_content is None:
        return ["冻结 GIS Brotli 压缩门禁无法完成。"]
    errors = []
    if len(gzip_content) >= GIS_COMPRESSED_MAX_BYTES:
        errors.append("冻结 GIS gzip 体积必须小于 300 KiB。")
    if len(brotli_content) >= GIS_COMPRESSED_MAX_BYTES:
        errors.append("冻结 GIS Brotli 体积必须小于 300 KiB。")
    return errors


def _validate_wechat_release_integrity(values, repo_root: Path):
    """把正式冻结记录绑定到干净 HEAD 及该提交内的五份发布材料。"""
    errors = []
    try:
        expected_root = repo_root.resolve(strict=True)
    except OSError:
        return ["正式发布的 Git 工作树无法验证。"]

    discovered_root = _run_git(expected_root, "rev-parse", "--show-toplevel")
    if discovered_root is None:
        return ["正式发布的 Git 工作树无法验证。"]
    try:
        actual_root = Path(discovered_root.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeDecodeError):
        return ["正式发布的 Git 工作树无法验证。"]
    if actual_root != expected_root:
        return ["正式发布校验必须指向 Git 工作树根目录。"]

    status = _run_git(
        expected_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status is None:
        errors.append("正式发布的 Git 工作树状态无法验证。")
    elif status:
        errors.append("正式发布要求 Git 工作树保持干净，检测到待提交内容。")

    head = _run_git(expected_root, "rev-parse", "--verify", "HEAD^{commit}")
    if head is None:
        errors.append("正式发布的 Git HEAD 无法验证。")
        return errors
    try:
        current_head = head.decode("ascii").strip()
    except UnicodeDecodeError:
        errors.append("正式发布的 Git HEAD 无法验证。")
        return errors
    if not LOWER_COMMIT_SHA_PATTERN.fullmatch(current_head):
        errors.append("正式发布的 Git HEAD 无法验证。")
        return errors
    target_commit = values.get("WECHAT_TARGET_COMMIT_SHA", "")
    if LOWER_COMMIT_SHA_PATTERN.fullmatch(target_commit):
        if current_head != target_commit:
            errors.append("WECHAT_TARGET_COMMIT_SHA 与当前 Git HEAD 不一致。")

    gis_content = _run_git(
        expected_root,
        "cat-file",
        "blob",
        f"{current_head}:{GIS_FROZEN_ARTIFACT_PATH}",
    )
    if gis_content is None:
        errors.append("冻结 GIS 文件无法从当前 Git HEAD 验证。")
    else:
        errors.extend(_validate_gis_compressed_content(gis_content))

    for key, relative_path in WECHAT_RELEASE_ARTIFACTS:
        expected_digest = values.get(key, "")
        content = _run_git(
            expected_root,
            "cat-file",
            "blob",
            f"{current_head}:{relative_path}",
        )
        if content is None:
            errors.append(f"{key} 对应的发布材料无法从当前 Git HEAD 验证。")
            continue
        if (
            LOWER_SHA256_PATTERN.fullmatch(expected_digest)
            and hashlib.sha256(content).hexdigest() != expected_digest
        ):
            errors.append(f"{key} 与当前 Git HEAD 中的发布材料不一致。")
        try:
            artifact_text = content.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{key} 对应的发布材料必须是 UTF-8 文本。")
            continue
        if WECHAT_RELEASE_CANDIDATE_MARKER in artifact_text:
            errors.append(f"{key} 对应的发布材料仍含候选占位标记。")
        status_markers = WECHAT_RELEASE_STATUS_MARKER_PATTERN.findall(artifact_text)
        if status_markers != [WECHAT_RELEASE_FINAL_STATUS]:
            errors.append(f"{key} 缺少唯一且明确的正式发布状态 marker。")
        visible_text = _visible_release_body(artifact_text)
        miniprogram_name = values.get("WECHAT_MINIPROGRAM_NAME", "")
        name_markers = WECHAT_MINIPROGRAM_NAME_MARKER_PATTERN.findall(artifact_text)
        if name_markers != [miniprogram_name]:
            errors.append(f"WECHAT_MINIPROGRAM_NAME 与 {key} 的名称 marker 不一致。")
        else:
            if miniprogram_name not in visible_text:
                errors.append(
                    f"WECHAT_MINIPROGRAM_NAME 未在 {key} 的可见正文中出现。"
                )
        if key in WECHAT_EFFECTIVE_DATE_ARTIFACT_KEYS:
            effective_date_markers = WECHAT_EFFECTIVE_DATE_MARKER_PATTERN.findall(
                artifact_text
            )
            if (
                len(effective_date_markers) != 1
                or effective_date_markers[0] != values.get("WECHAT_EFFECTIVE_DATE", "")
            ):
                errors.append(f"WECHAT_EFFECTIVE_DATE 与 {key} 的生效日期 marker 不一致。")
            visible_effective_dates = WECHAT_VISIBLE_EFFECTIVE_DATE_PATTERN.findall(
                visible_text
            )
            if (
                len(visible_effective_dates) != 1
                or visible_effective_dates[0]
                != values.get("WECHAT_EFFECTIVE_DATE", "")
            ):
                errors.append(
                    f"WECHAT_EFFECTIVE_DATE 与 {key} 的唯一可见生效日期不一致。"
                )
        if key in WECHAT_PRIVACY_VERSION_ARTIFACT_KEYS:
            privacy_version_markers = WECHAT_PRIVACY_VERSION_MARKER_PATTERN.findall(
                artifact_text
            )
            if (
                len(privacy_version_markers) != 1
                or privacy_version_markers[0]
                != values.get("WX_MINIPROGRAM_PRIVACY_VERSION", "")
            ):
                errors.append(
                    f"WX_MINIPROGRAM_PRIVACY_VERSION 与 {key} 的隐私版本 marker 不一致。"
                )
            visible_privacy_versions = WECHAT_VISIBLE_PRIVACY_VERSION_PATTERN.findall(
                visible_text
            )
            if (
                len(visible_privacy_versions) != 1
                or visible_privacy_versions[0]
                != values.get("WX_MINIPROGRAM_PRIVACY_VERSION", "")
            ):
                errors.append(
                    "WX_MINIPROGRAM_PRIVACY_VERSION 与 "
                    f"{key} 的唯一可见隐私版本不一致。"
                )

    project_config = _run_git(
        expected_root,
        "cat-file",
        "blob",
        f"{current_head}:{WECHAT_PROJECT_CONFIG_PATH}",
    )
    if project_config is None:
        errors.append("WX_MINIPROGRAM_APPID 无法与当前 Git HEAD 的工程配置核对。")
    else:
        try:
            project_values = json.loads(project_config)
            project_appid = project_values["appid"]
            if not isinstance(project_appid, str):
                raise KeyError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            errors.append("WX_MINIPROGRAM_APPID 无法与当前 Git HEAD 的工程配置核对。")
        else:
            if project_appid != WECHAT_PROJECT_CONFIG_PLACEHOLDER_APPID:
                errors.append("当前 Git HEAD 的工程配置必须固定为游客占位 AppID。")

    errors.extend(_validate_wechat_private_project_config(values, expected_root))

    miniprogram_config = _run_git(
        expected_root,
        "cat-file",
        "blob",
        f"{current_head}:{WECHAT_MINIPROGRAM_CONFIG_PATH}",
    )
    if miniprogram_config is None:
        errors.append(
            "WX_MINIPROGRAM_PRIVACY_VERSION 无法与当前 Git HEAD 的小程序配置核对。"
        )
    else:
        try:
            config_text = miniprogram_config.decode("utf-8")
        except UnicodeDecodeError:
            config_text = ""
        privacy_versions = [
            match.group(2)
            for match in PRIVACY_CONSENT_VERSION_PATTERN.finditer(config_text)
        ]
        if len(privacy_versions) != 1:
            errors.append(
                "WX_MINIPROGRAM_PRIVACY_VERSION 无法与当前 Git HEAD 的小程序配置核对。"
            )
        elif privacy_versions[0] != values.get("WX_MINIPROGRAM_PRIVACY_VERSION", ""):
            errors.append(
                "WX_MINIPROGRAM_PRIVACY_VERSION 与当前 Git HEAD 的小程序配置不一致。"
            )

    runtime_config = _run_git(
        expected_root,
        "cat-file",
        "blob",
        f"{current_head}:{WECHAT_MINIPROGRAM_RUNTIME_CONFIG_PATH}",
    )
    if runtime_config is None:
        errors.append("WECHAT_REQUEST_DOMAIN 无法与当前 Git HEAD 的运行时配置核对。")
    else:
        try:
            runtime_text = runtime_config.decode("utf-8")
        except UnicodeDecodeError:
            runtime_text = ""
        api_base_urls = [
            match.group(2) for match in API_BASE_URL_PATTERN.finditer(runtime_text)
        ]
        if len(api_base_urls) != 1:
            errors.append("WECHAT_REQUEST_DOMAIN 在当前 Git HEAD 中必须有唯一真实定义。")
        elif api_base_urls[0] != values.get("WECHAT_REQUEST_DOMAIN", ""):
            errors.append("WECHAT_REQUEST_DOMAIN 与当前 Git HEAD 的运行时配置不一致。")
    return errors


def _write_verified_commit(path: Path, commit: str):
    """把同一次校验确认的 commit 写入仅当前用户可读的全新票据。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(path, flags, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="ascii") as output:
        output.write(commit + "\n")


def snapshot_wechat_release_form(source: Path, destination: Path):
    """通过不跟随符号链接的文件描述符创建单次发布表单快照。"""
    content, read_error = _read_regular_file_stably(
        source,
        max_bytes=WECHAT_RELEASE_FORM_MAX_BYTES,
        require_private=True,
    )
    if read_error == "permission":
        return ["微信发布私密表单权限必须为 0600，请先执行 chmod 600。"]
    if read_error == "type":
        return ["微信发布私密表单必须是普通文件，不能使用符号链接。"]
    if read_error == "size":
        return ["微信发布私密表单大小异常。"]
    if read_error == "changed":
        return ["微信发布私密表单读取期间发生变化，请重新执行。"]
    if read_error is not None:
        return ["微信发布私密表单无法安全创建本机快照。"]

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    destination_fd = None
    destination_created = False
    errors = []
    try:
        destination_fd = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | no_follow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        destination_created = True
        view = memoryview(content)
        while view:
            written = os.write(destination_fd, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(destination_fd)
    except OSError:
        errors.append("微信发布私密表单无法安全创建本机快照。")
    finally:
        if destination_fd is not None:
            os.close(destination_fd)

    if errors and destination_created:
        try:
            os.unlink(destination)
        except OSError:
            pass
    return errors


def validate_wechat_release_form(
    path: Path,
    *,
    require_ready=False,
    repo_root: Path | None = None,
    verified_commit_output: Path | None = None,
    validation_time: datetime | None = None,
):
    """校验本机私密发布表单，只返回状态与错误，不返回任何填写值。"""
    errors = []
    warnings = []
    content, read_error = _read_regular_file_stably(
        path,
        max_bytes=WECHAT_RELEASE_FORM_MAX_BYTES,
        require_private=True,
    )
    if read_error == "missing":
        message = "微信发布私密表单不存在，请复制 .env.wechat-release.example 后填写。"
        (errors if require_ready else warnings).append(message)
        return {
            "ok": not errors,
            "form_ready": False,
            "category_confirmed": False,
            "warnings": warnings,
            "errors": errors,
        }
    read_error_messages = {
        "type": "微信发布私密表单必须是普通文件，不能使用符号链接。",
        "permission": "微信发布私密表单权限必须为 0600，请先执行 chmod 600。",
        "size": "微信发布私密表单大小异常。",
        "changed": "微信发布私密表单读取期间发生变化，请重新执行。",
        "io": "微信发布私密表单无法安全读取。",
    }
    if read_error is not None:
        errors.append(read_error_messages.get(read_error, "微信发布私密表单无法安全读取。"))
        return {
            "ok": False,
            "form_ready": False,
            "category_confirmed": False,
            "warnings": warnings,
            "errors": errors,
        }
    try:
        values = _parse_env(content)
    except UnicodeDecodeError:
        return {
            "ok": False,
            "form_ready": False,
            "category_confirmed": False,
            "warnings": [],
            "errors": ["微信发布私密表单必须是 UTF-8 文本。"],
        }
    form_ready = values.get("WECHAT_FORM_READY") == "1"
    category_confirmed = values.get("WECHAT_CATEGORY_CONFIRMED") == "1"
    if values.get("WECHAT_SUBJECT_TYPE") != "personal":
        errors.append("WECHAT_SUBJECT_TYPE 必须保持 personal。")
    if values.get("WECHAT_FORM_READY", "0") not in {"0", "1"}:
        errors.append("WECHAT_FORM_READY 只能是 0 或 1。")
    if values.get("WECHAT_CATEGORY_CONFIRMED", "0") not in {"0", "1"}:
        errors.append("WECHAT_CATEGORY_CONFIRMED 只能是 0 或 1。")
    if values.get("FEATURE_HEAT_EXPOSURE_GIS", "") not in {"", "0", "1"}:
        errors.append("FEATURE_HEAT_EXPOSURE_GIS 只能是 0 或 1。")
    if values.get("FEATURE_WXPUSHER", "") not in {"", "0", "1"}:
        errors.append("FEATURE_WXPUSHER 只能是 0 或 1。")

    must_be_complete = require_ready or form_ready
    category_evidence_present = any(
        values.get(key) for key in WECHAT_CATEGORY_EVIDENCE_KEYS
    )
    must_validate_category_evidence = (
        must_be_complete or category_confirmed or category_evidence_present
    )
    release_freeze_present = any(
        values.get(key) for key in WECHAT_RELEASE_FREEZE_KEYS
    )
    must_validate_release_freeze = must_be_complete or release_freeze_present
    if require_ready and not form_ready:
        errors.append("正式发布前必须将 WECHAT_FORM_READY 设为 1。")
    if must_be_complete and not category_confirmed:
        errors.append("正式发布前必须在后台确认个人主体可用类目，并设置 WECHAT_CATEGORY_CONFIRMED=1。")
    if must_be_complete:
        missing = [key for key in WECHAT_FORM_REQUIRED_KEYS if not values.get(key)]
        if missing:
            errors.append("微信发布私密表单缺少必填字段: " + ", ".join(missing))
    baseline_fields_present = any(
        values.get(key)
        for key in (
            "QWEATHER_DEDICATED_CREDENTIAL_CONFIRMED",
            "QWEATHER_CONSOLE_USAGE_MONTH",
            "QWEATHER_CONSOLE_USAGE_BASELINE",
        )
    )
    if must_be_complete or baseline_fields_present:
        errors.extend(
            _validate_qweather_console_baseline(
                values,
                validation_time=validation_time,
            )
        )

    if must_validate_category_evidence:
        missing_category_evidence = [
            key for key in WECHAT_CATEGORY_EVIDENCE_KEYS if not values.get(key)
        ]
        if missing_category_evidence and not must_be_complete:
            errors.append(
                "类目证据记录缺少必填字段: "
                + ", ".join(missing_category_evidence)
            )

        category_paths_json = values.get("WECHAT_CATEGORY_PATHS_JSON", "")
        if category_paths_json:
            errors.extend(_validate_wechat_category_paths_json(category_paths_json))

        qualification_status = values.get(
            "WECHAT_CATEGORY_QUALIFICATION_STATUS", ""
        )
        if qualification_status:
            if qualification_status != WECHAT_CATEGORY_NO_EXTRA_QUALIFICATION:
                errors.append(
                    "个人主体首发类目必须明确为无需额外机构资质；后台要求机构资质时不得发布。"
                )

        evidence_ref = values.get("WECHAT_CATEGORY_EVIDENCE_REF", "")
        evidence_ref_errors = []
        if evidence_ref:
            evidence_ref_errors = _validate_wechat_category_evidence_ref(evidence_ref)
            errors.extend(evidence_ref_errors)

        evidence_digest = values.get("WECHAT_CATEGORY_EVIDENCE_SHA256", "")
        evidence_digest_valid = bool(
            LOWER_SHA256_PATTERN.fullmatch(evidence_digest)
        )
        if evidence_digest and not evidence_digest_valid:
            errors.append(
                "WECHAT_CATEGORY_EVIDENCE_SHA256 必须是 64 位小写十六进制 SHA-256。"
            )

        confirmed_at = values.get("WECHAT_CATEGORY_CONFIRMED_AT", "")
        if confirmed_at:
            confirmed_at_errors = _validate_wechat_category_confirmed_at(confirmed_at)
            errors.extend(confirmed_at_errors)
            if must_be_complete and not confirmed_at_errors:
                errors.extend(
                    _validate_wechat_category_evidence_freshness(
                        confirmed_at,
                        validation_time=validation_time,
                    )
                )

        if (
            must_be_complete
            and values.get("WECHAT_CATEGORY_EVIDENCE_ROOT")
            and evidence_ref
            and not evidence_ref_errors
            and evidence_digest_valid
        ):
            errors.extend(
                _validate_wechat_category_evidence_file(
                    values,
                    repo_root if repo_root is not None else path.parent,
                )
            )

    if must_validate_release_freeze:
        missing_release_freeze = [
            key for key in WECHAT_RELEASE_FREEZE_KEYS if not values.get(key)
        ]
        if missing_release_freeze and not must_be_complete:
            errors.append(
                "发布冻结记录缺少必填字段: "
                + ", ".join(missing_release_freeze)
            )
        errors.extend(_validate_wechat_release_freeze(values))
        if must_be_complete:
            errors.extend(
                _validate_wechat_release_integrity(
                    values,
                    repo_root if repo_root is not None else path.parent,
                )
            )

    if must_be_complete:
        appid = values.get("WX_MINIPROGRAM_APPID", "")
        secret = values.get("WX_MINIPROGRAM_SECRET", "")
        if appid and not APPID_PATTERN.fullmatch(appid):
            errors.append("WX_MINIPROGRAM_APPID 格式异常。")
        if secret and len(secret) < 16:
            errors.append("WX_MINIPROGRAM_SECRET 长度异常。")
        wxpusher_token = values.get("WXPUSHER_APP_TOKEN", "")
        if wxpusher_token and not WXPUSHER_APP_TOKEN_PATTERN.fullmatch(
            wxpusher_token
        ):
            errors.append("WXPUSHER_APP_TOKEN 格式或长度异常。")
        if values.get("FEATURE_WXPUSHER") != "0":
            errors.append("1.0.0 正式首发必须固定 FEATURE_WXPUSHER=0。")
        if wxpusher_token:
            errors.append("FEATURE_WXPUSHER=0 时必须清空 WXPUSHER_APP_TOKEN。")
        contact_email = values.get("WECHAT_CONTACT_EMAIL", "")
        if contact_email and not EMAIL_PATTERN.fullmatch(contact_email):
            errors.append("WECHAT_CONTACT_EMAIL 格式异常。")
        request_domain = values.get("WECHAT_REQUEST_DOMAIN", "")
        if request_domain:
            errors.extend(_validate_wechat_request_domain(request_domain))
        effective_date = values.get("WECHAT_EFFECTIVE_DATE", "")
        if effective_date:
            try:
                date.fromisoformat(effective_date)
            except ValueError:
                errors.append("WECHAT_EFFECTIVE_DATE 必须使用 YYYY-MM-DD。")
        if len(values.get("WECHAT_OPERATOR_NAME", "")) > 80:
            errors.append("WECHAT_OPERATOR_NAME 长度异常。")
        if len(values.get("WECHAT_MINIPROGRAM_NAME", "")) > 80:
            errors.append("WECHAT_MINIPROGRAM_NAME 长度异常。")
        if values.get("FEATURE_HEAT_EXPOSURE_GIS") != "1":
            errors.append("微信正式发布必须启用 FEATURE_HEAT_EXPOSURE_GIS=1。")

    if must_be_complete and not errors and verified_commit_output is not None:
        try:
            _write_verified_commit(
                verified_commit_output,
                values["WECHAT_TARGET_COMMIT_SHA"],
            )
        except (KeyError, OSError):
            errors.append("已验证的目标提交无法安全写入本机临时票据。")

    if not form_ready and not require_ready:
        warnings.append("微信发布私密表单尚未完成，当前只能进行游客模式预览。")
    return {
        "ok": not errors,
        "form_ready": form_ready and not errors,
        "category_confirmed": category_confirmed,
        "warnings": warnings,
        "errors": errors,
    }


def _validate_qweather_base(api_base: str):
    errors = []
    try:
        parsed = urlparse(api_base)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ["QWEATHER_API_BASE URL 或端口格式异常。"]
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.params
    ):
        errors.append("QWEATHER_API_BASE 必须是无用户信息、查询参数和片段的 HTTPS URL。")
        return errors
    if not QWEATHER_API_AUTHORITY_PATTERN.fullmatch(parsed.netloc):
        errors.append("QWeather API Host 必须是控制台分配的 qweatherapi.com 子域名。")
    if port not in (None, 443):
        errors.append("QWEATHER_API_BASE 只允许标准 HTTPS 端口 443。")
    if parsed.path not in {"/v7", "/v7/"}:
        errors.append("QWeather API Base 路径必须为 /v7。")
    return errors


def _validate_qweather_private_key(path_value: str):
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        return ["QWEATHER_JWT_PRIVATE_KEY_PATH 必须是服务器上的绝对路径。"]
    _content, read_error = _read_regular_file_stably(
        path,
        max_bytes=QWEATHER_MAX_PRIVATE_KEY_BYTES,
        require_private=True,
        require_nonempty=True,
        required_mode=0o600,
    )
    if read_error == "missing":
        return ["QWeather JWT 私钥文件不存在或不可读取。"]
    if read_error == "type":
        return ["QWeather JWT 私钥必须是普通文件且不能是符号链接。"]
    if read_error == "permission":
        return ["QWeather JWT 私钥权限必须严格为 0600。"]
    if read_error == "size":
        return ["QWeather JWT 私钥文件大小异常。"]
    if read_error == "changed":
        return ["QWeather JWT 私钥读取期间发生变化，请重新执行。"]
    if read_error is not None:
        return ["QWeather JWT 私钥文件不存在或不可读取。"]
    return []


def validate_release_env(path: Path, *, require_wechat=False):
    content, read_error = _read_regular_file_stably(
        path,
        max_bytes=WECHAT_RELEASE_FORM_MAX_BYTES,
        require_private=False,
    )
    if read_error is not None:
        return {
            "ok": False,
            "wechat_ready": False,
            "weather_ready": False,
            "qweather_mode": "unknown",
            "wxpusher_ready": False,
            "warnings": [],
            "errors": ["候选发布环境文件无法安全读取。"],
        }
    try:
        values = _parse_env(content)
    except UnicodeDecodeError:
        return {
            "ok": False,
            "wechat_ready": False,
            "weather_ready": False,
            "qweather_mode": "unknown",
            "wxpusher_ready": False,
            "warnings": [],
            "errors": ["候选发布环境文件必须是 UTF-8 文本。"],
        }
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
    if require_wechat:
        errors.extend(_validate_deploy_dependency_lock())
        for key, expected_value in QWEATHER_FORMAL_PINNED_VALUES.items():
            if values.get(key, "") != expected_value:
                errors.append(f"微信正式模式必须固定 {key}={expected_value}。")
        errors.extend(_validate_qweather_console_baseline(values))
        if values.get("FEATURE_WEB_AI", "0") != "0":
            errors.append("微信正式发布必须关闭 FEATURE_WEB_AI。")
        if values.get("SILICONFLOW_API_KEY", ""):
            errors.append("微信正式发布必须清空 SILICONFLOW_API_KEY。")
        if values.get("SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1") != "https://api.siliconflow.cn/v1":
            errors.append("SILICONFLOW_API_BASE 必须固定为官方 HTTPS /v1 端点。")
        if public_base_url != WECHAT_EXPECTED_REQUEST_DOMAIN:
            errors.append("微信正式模式的 PUBLIC_BASE_URL 必须使用固定正式 origin。")
        if insecure_allowed:
            errors.append("微信正式模式禁止 ALLOW_INSECURE_PUBLIC_BASE_URL。")

        wxpusher_api_base = values.get("WXPUSHER_API_BASE", "")
        if wxpusher_api_base != WXPUSHER_EXPECTED_API_BASE:
            errors.append("微信正式模式的 WXPUSHER_API_BASE 必须使用固定官方 origin。")
        dispatch_lock_path = values.get("DISPATCH_LOCK_PATH", "")
        if (
            not dispatch_lock_path
            or not Path(dispatch_lock_path).is_absolute()
            or dispatch_lock_path == "/"
        ):
            errors.append("微信正式模式必须配置安全的绝对 DISPATCH_LOCK_PATH。")

    wechat_app_present = [key for key in WECHAT_APP_KEYS if values.get(key)]
    wechat_server_present = [key for key in WECHAT_SERVER_KEYS if values.get(key)]
    if wechat_app_present and len(wechat_app_present) != len(WECHAT_APP_KEYS):
        errors.append("WX_MINIPROGRAM_APPID 与 WX_MINIPROGRAM_SECRET 必须同时填写。")
    if wechat_server_present and len(wechat_server_present) != len(WECHAT_SERVER_KEYS):
        errors.append("微信身份 pepper 与会话密钥必须同时配置。")
    if wechat_server_present and not wechat_app_present:
        errors.append("微信服务端密钥不能脱离 AppID 与 AppSecret 单独启用。")

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
    persistent_budget_raw = values.get("QWEATHER_REQUIRE_PERSISTENT_BUDGET", "")
    persistent_budget_url = (
        values.get("WEATHER_CACHE_REDIS_URL", "")
        or values.get("REDIS_URL", "")
    )
    allow_weather_unavailable = values.get("ALLOW_WEATHER_UNAVAILABLE") == "1"
    weather_ready = False
    if persistent_budget_raw not in {"", "0", "1"}:
        errors.append("QWEATHER_REQUIRE_PERSISTENT_BUDGET 只能是 0 或 1。")
    if require_wechat and persistent_budget_raw != "1":
        errors.append("微信正式发布必须固定启用 QWeather 持久化预算。")
    if qweather_mode == "disabled":
        message = "和风天气同步当前停用，新服务器没有可用天气快照。"
        can_run_degraded_preview = allow_weather_unavailable and not require_wechat
        (warnings if can_run_degraded_preview else errors).append(message)
    elif qweather_mode == "api_key":
        if not values.get("QWEATHER_KEY") or not qweather_base:
            errors.append("QWEATHER_AUTH_MODE=api_key 时必须同时配置 Key 与 API Base。")
        else:
            mode_errors = _validate_qweather_base(qweather_base)
            mode_errors.extend(_validate_persistent_budget_url(persistent_budget_url))
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
            mode_errors = _validate_qweather_base(qweather_base)
            mode_errors.extend(_validate_persistent_budget_url(persistent_budget_url))
            mode_errors.extend(
                _validate_qweather_private_key(values["QWEATHER_JWT_PRIVATE_KEY_PATH"])
            )
            errors.extend(mode_errors)
            weather_ready = not mode_errors
    else:
        errors.append("QWEATHER_AUTH_MODE 只能是 disabled、api_key 或 jwt。")
    feature_heat_exposure_gis = values.get("FEATURE_HEAT_EXPOSURE_GIS", "")
    if feature_heat_exposure_gis not in {"", "0", "1"}:
        errors.append("FEATURE_HEAT_EXPOSURE_GIS 只能是 0 或 1。")
    elif require_wechat and feature_heat_exposure_gis != "1":
        errors.append("微信正式发布必须启用 FEATURE_HEAT_EXPOSURE_GIS=1。")
    feature_wxpusher = values.get("FEATURE_WXPUSHER", "0")
    if feature_wxpusher not in {"0", "1"}:
        errors.append("FEATURE_WXPUSHER 只能是 0 或 1。")
    wxpusher_token = values.get("WXPUSHER_APP_TOKEN", "")
    wxpusher_ready = bool(
        feature_wxpusher == "1"
        and wxpusher_token
        and WXPUSHER_APP_TOKEN_PATTERN.fullmatch(wxpusher_token)
    )
    if feature_wxpusher == "0" and wxpusher_token:
        errors.append("FEATURE_WXPUSHER=0 时必须清空 WXPUSHER_APP_TOKEN。")
    elif feature_wxpusher == "1" and not wxpusher_token:
        errors.append("FEATURE_WXPUSHER=1 时必须配置 WXPUSHER_APP_TOKEN。")
    elif wxpusher_token and not wxpusher_ready:
        errors.append("WXPUSHER_APP_TOKEN 格式或长度异常。")
    if require_wechat and feature_wxpusher != "0":
        errors.append("1.0.0 微信正式发布必须固定 FEATURE_WXPUSHER=0。")
    return {
        "ok": not errors,
        "wechat_ready": wechat_ready,
        "weather_ready": weather_ready,
        "qweather_mode": qweather_mode,
        "wxpusher_ready": wxpusher_ready,
        "warnings": warnings,
        "errors": errors,
    }


def _validate_persistent_budget_url(value):
    if not value:
        return ["QWeather 启用时必须配置 REDIS_URL 或 WEATHER_CACHE_REDIS_URL。"]
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("invalid redis url")
    except ValueError:
        return ["QWeather 持久化预算必须使用有效的 Redis URL。"]
    return []


def probe_persistent_budget_backend(path: Path, *, redis_module=None):
    """对候选环境的预算 Redis 执行连通性和 AOF 持久化探测。"""
    content, read_error = _read_regular_file_stably(
        path,
        max_bytes=WECHAT_RELEASE_FORM_MAX_BYTES,
        require_private=False,
    )
    if read_error is not None:
        return ["候选发布环境无法安全读取。"]
    try:
        values = _parse_env(content)
    except UnicodeDecodeError:
        return ["候选发布环境必须是 UTF-8 文本。"]
    mode = values.get("QWEATHER_AUTH_MODE", "disabled").lower()
    if mode == "disabled":
        return []
    redis_url = (
        values.get("WEATHER_CACHE_REDIS_URL", "")
        or values.get("REDIS_URL", "")
    )
    url_errors = _validate_persistent_budget_url(redis_url)
    if url_errors:
        return url_errors

    client = None
    try:
        if redis_module is None:
            import redis as redis_module  # pylint: disable=import-outside-toplevel
        client = redis_module.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        if client.ping() is not True:
            raise RuntimeError("redis ping returned non-true")
        persistence = client.info(section="persistence")
        if not isinstance(persistence, dict):
            raise RuntimeError("redis persistence info missing")
        if int(persistence.get("aof_enabled", -1)) != 1:
            raise RuntimeError("redis aof disabled")
        if int(persistence.get("loading", -1)) != 0:
            raise RuntimeError("redis is loading")
        if str(persistence.get("aof_last_write_status", "")).lower() != "ok":
            raise RuntimeError("redis aof write status unhealthy")
        rewrite_status = str(
            persistence.get("aof_last_bgrewrite_status", "")
        ).lower()
        if not rewrite_status or rewrite_status == "err":
            raise RuntimeError("redis aof rewrite status unhealthy")
        # CONFIG GET 权限不足时也关闭发布，避免无法证明 appendfsync 策略。
        appendfsync_config = client.config_get("appendfsync")
        if not isinstance(appendfsync_config, dict):
            raise RuntimeError("redis appendfsync config missing")
        appendfsync = appendfsync_config.get("appendfsync")
        if appendfsync is None:
            appendfsync = appendfsync_config.get(b"appendfsync")
        if isinstance(appendfsync, bytes):
            appendfsync = appendfsync.decode("ascii", errors="ignore")
        if str(appendfsync or "").lower() not in {"everysec", "always"}:
            raise RuntimeError("redis appendfsync policy unsafe")
    except Exception:
        return ["QWeather 持久化预算 Redis 连通性或 AOF 配置验证失败。"]
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return []


def seed_persistent_budget_baseline(
    path: Path,
    *,
    redis_module=None,
    validation_time: datetime | None = None,
):
    """把控制台当月已用量原子并入应用预算，现有计数只增不减。"""
    content, read_error = _read_regular_file_stably(
        path,
        max_bytes=WECHAT_RELEASE_FORM_MAX_BYTES,
        require_private=False,
    )
    if read_error is not None:
        return ["候选发布环境无法安全读取。"]
    try:
        values = _parse_env(content)
    except UnicodeDecodeError:
        return ["候选发布环境必须是 UTF-8 文本。"]
    if values.get("QWEATHER_AUTH_MODE", "disabled").lower() == "disabled":
        return []
    baseline_errors = _validate_qweather_console_baseline(
        values,
        validation_time=validation_time,
    )
    if baseline_errors:
        return baseline_errors
    redis_url = (
        values.get("WEATHER_CACHE_REDIS_URL", "")
        or values.get("REDIS_URL", "")
    )
    url_errors = _validate_persistent_budget_url(redis_url)
    if url_errors:
        return url_errors

    usage_month = values["QWEATHER_CONSOLE_USAGE_MONTH"]
    baseline = int(values["QWEATHER_CONSOLE_USAGE_BASELINE"])
    prefix = f"{QWEATHER_REDIS_BUDGET_PREFIX}:{usage_month}"
    total_key = f"{prefix}:total"
    endpoint_key = f"{prefix}:endpoints"
    now = validation_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=now.tzinfo)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=now.tzinfo)
    ttl_seconds = max(int((next_month - now).total_seconds()) + 2 * 86400, 86400)
    script = """
local baseline = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local total_raw = redis.call('GET', KEYS[1])
local total = tonumber(total_raw or '0')
if not total_raw or total < baseline then
  redis.call('SET', KEYS[1], baseline)
  total = baseline
end
local recorded_raw = redis.call('HGET', KEYS[2], '__console_baseline__')
local recorded = tonumber(recorded_raw or '0')
if not recorded_raw or recorded < baseline then
  redis.call('HSET', KEYS[2], '__console_baseline__', baseline)
end
if redis.call('TTL', KEYS[1]) < ttl then redis.call('EXPIRE', KEYS[1], ttl) end
if redis.call('TTL', KEYS[2]) < ttl then redis.call('EXPIRE', KEYS[2], ttl) end
return total
"""
    client = None
    try:
        if redis_module is None:
            import redis as redis_module  # pylint: disable=import-outside-toplevel
        client = redis_module.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        seeded_total = int(
            client.eval(
                script,
                2,
                total_key,
                endpoint_key,
                str(baseline),
                str(ttl_seconds),
            )
        )
        if seeded_total < baseline:
            raise RuntimeError("redis baseline decreased")
    except Exception:
        return ["QWeather 控制台用量基线写入持久化预算失败。"]
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate staged release environment.")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--wechat-form", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--verified-commit-output", type=Path)
    parser.add_argument("--snapshot-output", type=Path)
    parser.add_argument("--form-only", action="store_true")
    parser.add_argument("--probe-persistent-budget", action="store_true")
    parser.add_argument("--seed-persistent-budget", action="store_true")
    parser.add_argument("--require-wechat", choices=("0", "1"), default="0")
    args = parser.parse_args(argv)
    require_wechat = args.require_wechat == "1"
    if args.form_only:
        if not args.wechat_form:
            parser.error("--form-only 必须同时提供 --wechat-form")
        form_path = args.wechat_form
        if args.snapshot_output is not None:
            snapshot_errors = snapshot_wechat_release_form(
                args.wechat_form,
                args.snapshot_output,
            )
            if snapshot_errors:
                result = {
                    "ok": False,
                    "form_ready": False,
                    "category_confirmed": False,
                    "warnings": [],
                    "errors": snapshot_errors,
                }
            else:
                form_path = args.snapshot_output
                result = validate_wechat_release_form(
                    form_path,
                    require_ready=require_wechat,
                    repo_root=args.repo_root,
                    verified_commit_output=args.verified_commit_output,
                )
        else:
            result = validate_wechat_release_form(
                form_path,
                require_ready=require_wechat,
                repo_root=args.repo_root,
                verified_commit_output=args.verified_commit_output,
            )
    else:
        if not args.file:
            parser.error("必须提供 --file")
        result = validate_release_env(args.file, require_wechat=require_wechat)
        if args.probe_persistent_budget and result["ok"]:
            probe_errors = probe_persistent_budget_backend(args.file)
            if probe_errors:
                result["errors"].extend(probe_errors)
                result["ok"] = False
        if args.seed_persistent_budget and result["ok"]:
            seed_errors = seed_persistent_budget_baseline(args.file)
            if seed_errors:
                result["errors"].extend(seed_errors)
                result["ok"] = False
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
