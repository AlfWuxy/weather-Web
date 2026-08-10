#!/usr/bin/env python3
"""扫描 Git 已跟踪文本，阻止正式凭据进入公开仓库。"""

from __future__ import annotations

import math
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


SECRET_KEYS = {
    "QWEATHER_API_KEY",
    "QWEATHER_KEY",
    "WECHAT_MINIPROGRAM_APPSECRET",
    "WECHAT_MINIPROGRAM_APP_SECRET",
    "WX_APP_SECRET",
    "WX_MINIPROGRAM_SECRET",
    "WXPUSHER_APP_TOKEN",
}

PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "dummy",
    "example",
    "fake",
    "invalid",
    "mock",
    "placeholder",
    "preflight",
    "redacted",
    "replace",
    "sample",
    "test",
    "your-",
    "your_",
    "xxxx",
)

APP_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])wx[0-9A-Fa-f]{16}(?![A-Za-z0-9])")
WXPUSHER_PATTERN = re.compile(r"(?<![A-Za-z0-9_])AT_[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |ED25519 )?PRIVATE KEY-----")
QWEATHER_HOST_PATTERN = re.compile(
    r"https://([a-z0-9]{8,})\.[a-z0-9-]{2,20}\.qweatherapi\.com",
    re.IGNORECASE,
)
ENV_ASSIGNMENT_PATTERN = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?(?P<key>[A-Z][A-Z0-9_]*)[ \t]*=[ \t]*(?P<value>[^#\r\n]*)$"
)
MAPPING_ASSIGNMENT_PATTERN = re.compile(
    r"(?m)[\"'](?P<key>[A-Z][A-Z0-9_]*)[\"']\s*:\s*[\"'](?P<value>[^\"']*)[\"']"
)
INLINE_ASSIGNMENT_PATTERN = re.compile(
    r"(?m)\b(?P<key>[A-Z][A-Z0-9_]*)[ \t]*=[ \t]*[\"'`]?"
    r"(?P<value>[^\s\"'`,;\]}]+)"
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _normalized_value(raw_value: str) -> str:
    value = raw_value.strip().strip("\"'`")
    return value.rstrip(",;])}").strip().strip("\"'`")


def _is_placeholder_or_dynamic(value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    if value.startswith("$") or "${" in value or "$(" in value:
        return True
    if value in {"None", "null", "nil"}:
        return True
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    return -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in counts.values()
    )


def _looks_like_literal_secret(value: str) -> bool:
    if _is_placeholder_or_dynamic(value):
        return False
    if len(value) < 20:
        return False
    has_letter = any(char.isalpha() for char in value)
    has_digit = any(char.isdigit() for char in value)
    return has_letter and has_digit and _shannon_entropy(value) >= 3.0


def scan_text(path: str, text: str) -> list[Finding]:
    """返回位置元数据，不回显疑似凭据正文。"""
    findings: list[Finding] = []

    for match in APP_ID_PATTERN.finditer(text):
        findings.append(Finding(path, _line_number(text, match.start()), "wechat_app_id"))

    for match in WXPUSHER_PATTERN.finditer(text):
        if not _is_placeholder_or_dynamic(match.group(0)):
            findings.append(Finding(path, _line_number(text, match.start()), "wxpusher_token"))

    for match in PRIVATE_KEY_PATTERN.finditer(text):
        findings.append(Finding(path, _line_number(text, match.start()), "private_key"))

    for match in QWEATHER_HOST_PATTERN.finditer(text):
        hostname_label = match.group(1).lower()
        if not any(marker in hostname_label for marker in PLACEHOLDER_MARKERS):
            findings.append(Finding(path, _line_number(text, match.start()), "qweather_api_host"))

    for pattern in (
        ENV_ASSIGNMENT_PATTERN,
        MAPPING_ASSIGNMENT_PATTERN,
        INLINE_ASSIGNMENT_PATTERN,
    ):
        for match in pattern.finditer(text):
            key = match.group("key")
            if key not in SECRET_KEYS:
                continue
            value = _normalized_value(match.group("value"))
            if _looks_like_literal_secret(value):
                findings.append(
                    Finding(path, _line_number(text, match.start("value")), f"literal_{key.lower()}")
                )

    return sorted(set(findings), key=lambda item: (item.path, item.line, item.kind))


def _tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def scan_tracked_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for file_path in _tracked_files(root):
        try:
            data = file_path.read_bytes()
        except FileNotFoundError:
            continue
        if b"\0" in data or len(data) > 5 * 1024 * 1024:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(file_path.relative_to(root).as_posix(), text))
    return sorted(set(findings), key=lambda item: (item.path, item.line, item.kind))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        findings = scan_tracked_files(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"密钥门禁无法完成：{exc}", file=sys.stderr)
        return 2

    if findings:
        print("发现疑似正式凭据。输出已隐藏凭据正文：", file=sys.stderr)
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.kind}", file=sys.stderr)
        return 1

    print("已跟踪文件密钥门禁通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
