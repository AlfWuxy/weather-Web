#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宜老平安小程序与宜老天气通服务发布内容的纯确定性合同。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Mapping


EXPECTED_PLATFORM_NAME = "宜老平安"
EXPECTED_SERVICE_NAME = "宜老天气通"
# 兼容仍把 EXPECTED_NAME 作为微信平台正式名称读取的仓库内调用方。
EXPECTED_NAME = EXPECTED_PLATFORM_NAME
EXPECTED_RELEASE_VERSION = "1.0.0"
PRIVACY_DOC_PATH = "docs/miniprogram/PRIVACY_NOTICE_TEMPLATE.md"
AGREEMENT_DOC_PATH = "docs/miniprogram/USER_AGREEMENT_TEMPLATE.md"
LISTING_COPY_PATH = "docs/miniprogram/LISTING_COPY.md"
PRIVACY_PAGE_PATH = "miniprogram/pages/privacy/index.wxml"
AGREEMENT_PAGE_PATH = "miniprogram/pages/agreement/index.wxml"
HEALTH_CONSENT_PAGE_PATH = "miniprogram/pages/health-consent/index.wxml"
CONFIG_PATH = "miniprogram/config.js"
RELEASE_ARTIFACTS = (
    ("WECHAT_PRIVACY_DOC_SHA256", PRIVACY_DOC_PATH),
    ("WECHAT_AGREEMENT_DOC_SHA256", AGREEMENT_DOC_PATH),
    ("WECHAT_LISTING_COPY_SHA256", LISTING_COPY_PATH),
    ("WECHAT_PRIVACY_PAGE_SHA256", PRIVACY_PAGE_PATH),
    ("WECHAT_AGREEMENT_PAGE_SHA256", AGREEMENT_PAGE_PATH),
    ("WECHAT_HEALTH_CONSENT_PAGE_SHA256", HEALTH_CONSENT_PAGE_PATH),
)
CONTENT_PATHS = tuple(path for _, path in RELEASE_ARTIFACTS) + (CONFIG_PATH,)
CANDIDATE_SHA256 = {
    PRIVACY_DOC_PATH: "9b5417fde70291975bffb3126503243c0172bdd4094a8e37d0a9cf783019e4bf",
    AGREEMENT_DOC_PATH: "fc8a42d578933b3a3dc575170455cb9cf8959ca02b64e02aeaf5d7284ba5c107",
    LISTING_COPY_PATH: "983de96df086c75c7f368f5abdb64cee4c798f0affde5ba5e77aba7918262aa7",
    PRIVACY_PAGE_PATH: "03d0cc5f012bade9d138c74eaced00a679c66c6f91c3496a2c9a6f278339cc0a",
    AGREEMENT_PAGE_PATH: "6f2d81e4a0281e207ed32d5c65cfc1ee96e9b48994fb3589e950be060ac19f80",
    HEALTH_CONSENT_PAGE_PATH: "e79b956b29b09bccc024a2c0c6ae597f6fdbd3a601f5859dd63d220b09408bc0",
    CONFIG_PATH: "c8995ca740ab477fada9b68817b6d4fb99d92a77ffccd7fe34008b3379a956b3",
}
FREEZE_KEYS = ("WECHAT_RELEASE_VERSION", "WECHAT_TARGET_COMMIT_SHA") + tuple(
    key for key, _ in RELEASE_ARTIFACTS
)

PRIVACY_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
CONFIG_RE = re.compile(
    r"^\s*PRIVACY_CONSENT_VERSION\s*:\s*(['\"])([^'\"\r\n]+)\1\s*,?\s*$",
    re.MULTILINE,
)
STATUS_RE = re.compile(r"^<!-- WECHAT_RELEASE_STATUS: ([a-z]+) -->$", re.MULTILINE)
NAME_RE = re.compile(r"^<!-- WECHAT_MINIPROGRAM_NAME: ([^<>\r\n]+) -->$", re.MULTILINE)
SERVICE_NAME_RE = re.compile(r"^<!-- WECHAT_SERVICE_NAME: ([^<>\r\n]+) -->$", re.MULTILINE)
DATE_RE = re.compile(r"^<!-- WECHAT_EFFECTIVE_DATE: (\d{4}-\d{2}-\d{2}) -->$", re.MULTILINE)
PRIVACY_RE = re.compile(r"^<!-- WECHAT_PRIVACY_VERSION: ([A-Za-z0-9._-]+) -->$", re.MULTILINE)
COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1\s*>", re.I)


class ReleaseContractError(ValueError):
    """发布内容或公开字段不满足确定性合同。"""


@dataclass(frozen=True)
class PublicReleaseFields:
    name: str
    service_name: str
    effective_date: str
    privacy_version: str
    release_version: str

    @property
    def visible_brand_relation(self) -> str:
        """返回六份正式材料统一展示的平台名与服务名关系。"""
        return f"{self.name}小程序 · {self.service_name}服务"


def validate_public_fields(
    fields: PublicReleaseFields,
    *,
    form_ready: str,
    category_confirmed: str,
) -> None:
    if fields.name != EXPECTED_PLATFORM_NAME:
        raise ReleaseContractError("小程序名称不符合发布合同。")
    if fields.service_name != EXPECTED_SERVICE_NAME:
        raise ReleaseContractError("小程序内服务名称不符合发布合同。")
    if fields.release_version != EXPECTED_RELEASE_VERSION:
        raise ReleaseContractError("首发版本不符合发布合同。")
    try:
        if date.fromisoformat(fields.effective_date).isoformat() != fields.effective_date:
            raise ValueError
    except ValueError as error:
        raise ReleaseContractError("生效日期格式不符合发布合同。") from error
    if not PRIVACY_VERSION_RE.fullmatch(fields.privacy_version):
        raise ReleaseContractError("隐私版本格式不符合发布合同。")
    if form_ready != "0" or category_confirmed != "0":
        raise ReleaseContractError("发布与类目门禁必须保持为 0。")


def _text(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseContractError("发布材料必须是 UTF-8 文本。") from error


def _replace(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ReleaseContractError("候选材料锚点不符合发布合同。")
    return text.replace(old, new)


def _markers(fields: PublicReleaseFields, *, dated: bool = False, private: bool = False) -> str:
    lines = [
        "<!-- WECHAT_RELEASE_STATUS: final -->",
        f"<!-- WECHAT_MINIPROGRAM_NAME: {fields.name} -->",
        f"<!-- WECHAT_SERVICE_NAME: {fields.service_name} -->",
    ]
    if dated:
        lines.append(f"<!-- WECHAT_EFFECTIVE_DATE: {fields.effective_date} -->")
    if private:
        lines.append(f"<!-- WECHAT_PRIVACY_VERSION: {fields.privacy_version} -->")
    return "\n".join(lines)


def render_artifact(path: str, content: bytes, fields: PublicReleaseFields) -> bytes:
    """把一份候选 blob 前向渲染为最终 blob。"""
    text = _text(content)
    if path == PRIVACY_DOC_PATH:
        text = _replace(text, "# 宜老天气通隐私说明发布候选版\n\n> 候选隐私版本：`2026-07-18`。本文尚未作为正式生效文本。正式提交审核时需要根据微信公众平台后台实际勾选的数据类型、接口权限、主体名称和联系方式逐项校对，并同时冻结生效日期、`WX_MINIPROGRAM_PRIVACY_VERSION`、目标 commit hash 和对应页面内容 hash；四者写入私有发布确认单后才可标记为正式版。", f"# {fields.name}隐私说明\n\n{_markers(fields, dated=True, private=True)}\n\n{fields.visible_brand_relation}\n\n生效日期：{fields.effective_date}\n隐私版本：{fields.privacy_version}")
        text = _replace(text, "正式提交前，将认证主体姓名、专用联系邮箱和生效日期同步填写到微信公众平台隐私保护指引与本机私密发布表单；仓库不保存个人证件或认证隐私材料。", "认证运营者姓名、专用联系邮箱和本说明生效日期以微信公众平台隐私保护指引展示的信息为准；仓库不保存个人证件或认证隐私材料。")
    elif path == AGREEMENT_DOC_PATH:
        text = _replace(text, "# 宜老天气通用户协议发布候选版\n\n候选版本：`2026-07-18`。正式提交审核时同步冻结生效日期、协议版本、隐私版本、目标 commit hash 和对应页面内容 hash；冻结前本文不作为正式生效版本。", f"# {fields.name}用户协议\n\n{_markers(fields, dated=True)}\n\n{fields.visible_brand_relation}\n\n生效日期：{fields.effective_date}")
        text = _replace(text, "正式提交前，将微信认证的个人主体姓名和专用联系邮箱同步填写到微信公众平台隐私保护指引与审核材料中。仓库不保存个人证件、AppSecret 或其他认证隐私材料。", "认证运营者姓名和专用联系邮箱以微信公众平台隐私保护指引展示的信息为准。仓库不保存个人证件、AppSecret 或其他认证隐私材料。")
    elif path == LISTING_COPY_PATH:
        text = _replace(text, "# 微信小程序上架文案与审核路径（发布候选版）\n\n> 本文案随候选版本迭代。正式提交审核时，同时冻结生效日期、版本号、目标 commit hash、隐私版本及各提交页面内容 hash，并写入私有发布确认单。", f"# 微信小程序上架文案与审核路径\n\n{_markers(fields)}\n\n{fields.visible_brand_relation}\n\n> 本文案对应正式首发版本 `{fields.release_version}`，审核路径与当前提交功能保持一致。")
        text = _replace(text, "- 小程序名称：宜老天气通", f"- 小程序名称：{fields.name}\n- 小程序内服务名称：{fields.service_name}")
        for old, new in (("- 建议版本号：`1.0.0`", f"- 版本号：`{fields.release_version}`"), ("类目必须覆盖候选包的完整真实功能", "类目必须覆盖本次提交包的完整真实功能"), ("首个微信小程序发布候选版。", "首个微信小程序正式首发版。")):
            text = _replace(text, old, new)
    elif path == PRIVACY_PAGE_PATH:
        text = _replace(text, '<view class="page-shell">', f"{_markers(fields, dated=True, private=True)}\n<view class=\"page-shell\">")
        text = _replace(text, '<view class="hero-kicker">隐私与数据边界 · 发布候选版</view>', f'<view class="hero-kicker">{fields.visible_brand_relation} · 隐私与数据边界</view>')
        text = _replace(text, "运营者姓名、专用联系邮箱与生效日期会在正式提交前同步到微信平台隐私保护指引，以平台展示的认证信息为准。", "运营者姓名、专用联系邮箱与生效日期以微信公众平台隐私保护指引展示的认证信息为准。")
        text = _replace(text, '<view class="privacy-version">候选版本：2026-07-18 · 正式提交审核时同步冻结生效日期、隐私版本、目标 commit hash 和页面内容 hash；重要变化会再次请你阅读并主动同意。</view>', f'<view class="privacy-version">生效日期：{fields.effective_date} · 隐私版本：{fields.privacy_version} · 重要变化会再次请你阅读并主动同意。</view>')
    elif path == AGREEMENT_PAGE_PATH:
        text = _replace(text, '<view class="page-shell">', f"{_markers(fields, dated=True)}\n<view class=\"page-shell\">")
        text = _replace(text, '<view class="hero-kicker">宜老天气通用户协议 · 发布候选版</view>', f'<view class="hero-kicker">{fields.visible_brand_relation} · 用户协议</view>')
        text = _replace(text, '<view class="effective-date">候选版本：2026-07-18 · 正式提交审核时同步冻结生效日期、协议与隐私版本、目标 commit hash 和页面内容 hash。</view>', f'<view class="effective-date">生效日期：{fields.effective_date} · 重要协议或隐私规则变化会通过版本更新提示。</view>')
    elif path == HEALTH_CONSENT_PAGE_PATH:
        text = _replace(text, '<view class="page-shell">', f"{_markers(fields, dated=True, private=True)}\n<view class=\"page-shell\">")
        text = _replace(text, '<view class="hero-kicker">健康敏感个人信息</view>', f'<view class="hero-kicker">{fields.visible_brand_relation} · 健康敏感个人信息</view>')
        text = _replace(text, '<view class="version-text">当前服务端要求版本：{{requiredVersion}}</view>', f'<view class="version-text">生效日期：{fields.effective_date} · 隐私版本：{fields.privacy_version} · 当前服务端要求版本：{{{{requiredVersion}}}}</view>')
    elif path == CONFIG_PATH:
        matches = list(CONFIG_RE.finditer(text))
        if len(matches) != 1:
            raise ReleaseContractError("隐私版本配置锚点不符合发布合同。")
        match = matches[0]
        quote = match.group(1)
        text = text[: match.start()] + f"  PRIVACY_CONSENT_VERSION: {quote}{fields.privacy_version}{quote}," + text[match.end() :]
    else:
        raise ReleaseContractError("发布材料路径不符合发布合同。")
    return text.encode("utf-8")


def _restore_candidate(path: str, content: bytes, fields: PublicReleaseFields) -> bytes:
    """从正式文本严格恢复已人工复核的候选基线。"""
    text = _text(content)
    if path == PRIVACY_DOC_PATH:
        text = _replace(text, f"# {fields.name}隐私说明\n\n{_markers(fields, dated=True, private=True)}\n\n{fields.visible_brand_relation}\n\n生效日期：{fields.effective_date}\n隐私版本：{fields.privacy_version}", "# 宜老天气通隐私说明发布候选版\n\n> 候选隐私版本：`2026-07-18`。本文尚未作为正式生效文本。正式提交审核时需要根据微信公众平台后台实际勾选的数据类型、接口权限、主体名称和联系方式逐项校对，并同时冻结生效日期、`WX_MINIPROGRAM_PRIVACY_VERSION`、目标 commit hash 和对应页面内容 hash；四者写入私有发布确认单后才可标记为正式版。")
        text = _replace(text, "认证运营者姓名、专用联系邮箱和本说明生效日期以微信公众平台隐私保护指引展示的信息为准；仓库不保存个人证件或认证隐私材料。", "正式提交前，将认证主体姓名、专用联系邮箱和生效日期同步填写到微信公众平台隐私保护指引与本机私密发布表单；仓库不保存个人证件或认证隐私材料。")
    elif path == AGREEMENT_DOC_PATH:
        text = _replace(text, f"# {fields.name}用户协议\n\n{_markers(fields, dated=True)}\n\n{fields.visible_brand_relation}\n\n生效日期：{fields.effective_date}", "# 宜老天气通用户协议发布候选版\n\n候选版本：`2026-07-18`。正式提交审核时同步冻结生效日期、协议版本、隐私版本、目标 commit hash 和对应页面内容 hash；冻结前本文不作为正式生效版本。")
        text = _replace(text, "认证运营者姓名和专用联系邮箱以微信公众平台隐私保护指引展示的信息为准。仓库不保存个人证件、AppSecret 或其他认证隐私材料。", "正式提交前，将微信认证的个人主体姓名和专用联系邮箱同步填写到微信公众平台隐私保护指引与审核材料中。仓库不保存个人证件、AppSecret 或其他认证隐私材料。")
    elif path == LISTING_COPY_PATH:
        text = _replace(text, f"# 微信小程序上架文案与审核路径\n\n{_markers(fields)}\n\n{fields.visible_brand_relation}\n\n> 本文案对应正式首发版本 `{fields.release_version}`，审核路径与当前提交功能保持一致。", "# 微信小程序上架文案与审核路径（发布候选版）\n\n> 本文案随候选版本迭代。正式提交审核时，同时冻结生效日期、版本号、目标 commit hash、隐私版本及各提交页面内容 hash，并写入私有发布确认单。")
        text = _replace(text, f"- 小程序名称：{fields.name}\n- 小程序内服务名称：{fields.service_name}", "- 小程序名称：宜老天气通")
        for final, candidate in ((f"- 版本号：`{fields.release_version}`", "- 建议版本号：`1.0.0`"), ("类目必须覆盖本次提交包的完整真实功能", "类目必须覆盖候选包的完整真实功能"), ("首个微信小程序正式首发版。", "首个微信小程序发布候选版。")):
            text = _replace(text, final, candidate)
    elif path == PRIVACY_PAGE_PATH:
        text = _replace(text, f"{_markers(fields, dated=True, private=True)}\n<view class=\"page-shell\">", '<view class="page-shell">')
        text = _replace(text, f'<view class="hero-kicker">{fields.visible_brand_relation} · 隐私与数据边界</view>', '<view class="hero-kicker">隐私与数据边界 · 发布候选版</view>')
        text = _replace(text, "运营者姓名、专用联系邮箱与生效日期以微信公众平台隐私保护指引展示的认证信息为准。", "运营者姓名、专用联系邮箱与生效日期会在正式提交前同步到微信平台隐私保护指引，以平台展示的认证信息为准。")
        text = _replace(text, f'<view class="privacy-version">生效日期：{fields.effective_date} · 隐私版本：{fields.privacy_version} · 重要变化会再次请你阅读并主动同意。</view>', '<view class="privacy-version">候选版本：2026-07-18 · 正式提交审核时同步冻结生效日期、隐私版本、目标 commit hash 和页面内容 hash；重要变化会再次请你阅读并主动同意。</view>')
    elif path == AGREEMENT_PAGE_PATH:
        text = _replace(text, f"{_markers(fields, dated=True)}\n<view class=\"page-shell\">", '<view class="page-shell">')
        text = _replace(text, f'<view class="hero-kicker">{fields.visible_brand_relation} · 用户协议</view>', '<view class="hero-kicker">宜老天气通用户协议 · 发布候选版</view>')
        text = _replace(text, f'<view class="effective-date">生效日期：{fields.effective_date} · 重要协议或隐私规则变化会通过版本更新提示。</view>', '<view class="effective-date">候选版本：2026-07-18 · 正式提交审核时同步冻结生效日期、协议与隐私版本、目标 commit hash 和页面内容 hash。</view>')
    elif path == HEALTH_CONSENT_PAGE_PATH:
        text = _replace(text, f"{_markers(fields, dated=True, private=True)}\n<view class=\"page-shell\">", '<view class="page-shell">')
        text = _replace(text, f'<view class="hero-kicker">{fields.visible_brand_relation} · 健康敏感个人信息</view>', '<view class="hero-kicker">健康敏感个人信息</view>')
        text = _replace(text, f'<view class="version-text">生效日期：{fields.effective_date} · 隐私版本：{fields.privacy_version} · 当前服务端要求版本：{{{{requiredVersion}}}}</view>', '<view class="version-text">当前服务端要求版本：{{requiredVersion}}</view>')
    elif path == CONFIG_PATH:
        matches = list(CONFIG_RE.finditer(text))
        if len(matches) != 1:
            raise ReleaseContractError("隐私版本配置锚点不符合发布合同。")
        match = matches[0]
        text = text[: match.start()] + f"  PRIVACY_CONSENT_VERSION: {match.group(1)}2026-07-18{match.group(1)}," + text[match.end() :]
    else:
        raise ReleaseContractError("发布材料路径不符合发布合同。")
    return text.encode("utf-8")


def _verify_candidate(contents: Mapping[str, bytes]) -> None:
    if any(hashlib.sha256(contents[path]).hexdigest() != CANDIDATE_SHA256[path] for path in CONTENT_PATHS):
        raise ReleaseContractError("候选材料 SHA-256 不符合已审基线。")


def restore_candidate(contents: Mapping[str, bytes], fields: PublicReleaseFields) -> dict[str, bytes]:
    """把完整正式材料严格回溯为已人工复核的候选基线。"""
    if set(contents) != set(CONTENT_PATHS):
        raise ReleaseContractError("发布材料清单不完整。")
    restored = {path: _restore_candidate(path, contents[path], fields) for path in CONTENT_PATHS}
    _verify_candidate(restored)
    if any(render_artifact(path, restored[path], fields) != contents[path] for path in CONTENT_PATHS):
        raise ReleaseContractError("正式材料无法确定性回溯到候选基线。")
    return restored


def render_final(candidate: Mapping[str, bytes], fields: PublicReleaseFields) -> dict[str, bytes]:
    if set(candidate) != set(CONTENT_PATHS):
        raise ReleaseContractError("发布材料清单不完整。")
    if sum(_text(candidate[path]).count("候选") for path in CONTENT_PATHS[:-1]) != 12:
        raise ReleaseContractError("候选材料必须精确包含 12 个候选标记。")
    _verify_candidate(candidate)
    result = {path: render_artifact(path, candidate[path], fields) for path in CONTENT_PATHS}
    verify_final(result, fields)
    return result


def has_final_marker(content: bytes) -> bool:
    return STATUS_RE.findall(_text(content)) == ["final"]


def _visible(text: str) -> str:
    return SCRIPT_STYLE_RE.sub("", COMMENT_RE.sub("", text))


def verify_final(contents: Mapping[str, bytes], fields: PublicReleaseFields) -> None:
    """验证 26 个 marker、双名称可见关系和 config 同步合同。"""
    if set(contents) != set(CONTENT_PATHS):
        raise ReleaseContractError("发布材料清单不完整。")
    dated = {
        PRIVACY_DOC_PATH,
        AGREEMENT_DOC_PATH,
        PRIVACY_PAGE_PATH,
        AGREEMENT_PAGE_PATH,
        HEALTH_CONSENT_PAGE_PATH,
    }
    private = {PRIVACY_DOC_PATH, PRIVACY_PAGE_PATH, HEALTH_CONSENT_PAGE_PATH}
    marker_total = 0
    for path in CONTENT_PATHS[:-1]:
        text = _text(contents[path])
        visible = _visible(text)
        expected_date = [fields.effective_date] if path in dated else []
        expected_private = [fields.privacy_version] if path in private else []
        if (
            "候选" in text
            or STATUS_RE.findall(text) != ["final"]
            or NAME_RE.findall(text) != [fields.name]
            or SERVICE_NAME_RE.findall(text) != [fields.service_name]
        ):
            raise ReleaseContractError("正式材料状态或名称 marker 不一致。")
        if DATE_RE.findall(text) != expected_date or PRIVACY_RE.findall(text) != expected_private:
            raise ReleaseContractError("正式材料日期或隐私 marker 不一致。")
        if (
            visible.count(fields.visible_brand_relation) != 1
            or re.findall(r"生效日期：(\d{4}-\d{2}-\d{2})", visible) != expected_date
        ):
            raise ReleaseContractError("正式材料可见双名称关系或日期不一致。")
        if re.findall(r"隐私版本：([A-Za-z0-9._-]+)", visible) != expected_private:
            raise ReleaseContractError("正式材料可见隐私版本不一致。")
        marker_total += text.count("<!-- WECHAT_")
    listing = _visible(_text(contents[LISTING_COPY_PATH]))
    if listing.count(f"正式首发版本 `{fields.release_version}`") != 1 or listing.count(f"- 版本号：`{fields.release_version}`") != 1:
        raise ReleaseContractError("上架文案首发版本不一致。")
    versions = [match.group(2) for match in CONFIG_RE.finditer(_text(contents[CONFIG_PATH]))]
    if marker_total != 26 or versions != [fields.privacy_version]:
        raise ReleaseContractError("正式材料 marker 数量或 config 隐私版本不一致。")
    restore_candidate(contents, fields)
