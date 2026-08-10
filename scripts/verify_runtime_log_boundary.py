#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证正式站点不会通过 Nginx access log 留下访问者标识。"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


class LogBoundaryError(ValueError):
    """运行时日志边界不符合正式发布合同。"""


def _server_blocks(source: str) -> list[str]:
    """提取 Nginx server 块，忽略注释与引号中的花括号。"""
    blocks: list[str] = []
    masked = _mask_comments_and_strings(source)
    for match in re.finditer(r"\bserver\s*\{", masked):
        depth = 1
        cursor = match.end()
        while cursor < len(masked) and depth:
            if masked[cursor] == "{":
                depth += 1
            elif masked[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth:
            raise LogBoundaryError("Nginx 配置括号不完整。")
        blocks.append(source[match.start():cursor])
    return blocks


def _mask_comments_and_strings(source: str) -> str:
    """以空格遮盖注释和引号内容，同时保留原字符位置。"""
    masked = list(source)
    quote = None
    escaped = False
    in_comment = False
    for index, char in enumerate(source):
        if in_comment:
            if char == "\n":
                in_comment = False
            else:
                masked[index] = " "
            continue
        if quote:
            masked[index] = " "
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "#":
            masked[index] = " "
            in_comment = True
        elif char in {'"', "'"}:
            masked[index] = " "
            quote = char
    if quote:
        raise LogBoundaryError("Nginx 配置引号不完整。")
    return "".join(masked)


def _direct_directives(block: str) -> list[str]:
    """只提取 server 直接层级的指令，排除 location 等子块。"""
    masked = _mask_comments_and_strings(block)
    opening = masked.find("{")
    if opening < 0:
        raise LogBoundaryError("Nginx server 块缺少起始括号。")

    directives: list[str] = []
    depth = 1
    start = opening + 1
    cursor = start
    while cursor < len(masked) and depth:
        char = masked[cursor]
        if char == "{" and depth == 1:
            depth += 1
            start = cursor + 1
        elif char == "{":
            depth += 1
        elif char == "}" and depth == 2:
            depth -= 1
            start = cursor + 1
        elif char == "}":
            depth -= 1
        elif char == ";" and depth == 1:
            directive = masked[start:cursor].strip()
            if directive:
                directives.append(directive)
            start = cursor + 1
        cursor += 1
    if depth:
        raise LogBoundaryError("Nginx server 块括号不完整。")
    return directives


def _all_directives(block: str) -> list[str]:
    """提取 server 及全部子块指令，用于拒绝任何日志覆盖。"""
    masked = _mask_comments_and_strings(block)
    opening = masked.find("{")
    if opening < 0:
        raise LogBoundaryError("Nginx server 块缺少起始括号。")

    directives: list[str] = []
    start = opening + 1
    for cursor in range(start, len(masked)):
        char = masked[cursor]
        if char in "{}":
            start = cursor + 1
        elif char == ";":
            directive = masked[start:cursor].strip()
            if directive:
                directives.append(directive)
            start = cursor + 1
    return directives


def _values(directives: list[str], name: str) -> list[str]:
    """返回指定直接层级指令的参数。"""
    values = []
    for directive in directives:
        parts = directive.split(None, 1)
        if parts and parts[0] == name:
            values.append(parts[1].strip() if len(parts) == 2 else "")
    return values


def verify_nginx_source(source: str) -> None:
    """要求完整活动配置中的目标站点不生成访问者日志。"""
    parsed_blocks = [(_direct_directives(block), block) for block in _server_blocks(source)]
    target_blocks = []
    for directives, block in parsed_blocks:
        server_names = " ".join(_values(directives, "server_name")).split()
        if "yilaoweather.org" in server_names:
            target_blocks.append((directives, block))
    if len(target_blocks) != 1:
        raise LogBoundaryError("宜老天气站点 server 块数量异常。")

    directives, block = target_blocks[0]
    all_directives = _all_directives(block)
    if _values(all_directives, "include"):
        raise LogBoundaryError("宜老天气站点不得在 server 或子块中使用 include。")
    if _values(directives, "access_log") != ["off"]:
        raise LogBoundaryError("宜老天气站点必须只声明 access_log off。")
    if _values(all_directives, "access_log") != ["off"]:
        raise LogBoundaryError("宜老天气站点子块不得覆盖 access_log。")
    if _values(directives, "error_log") != ["/dev/null crit"]:
        raise LogBoundaryError("宜老天气站点必须把请求级 error_log 丢弃到 /dev/null。")
    if _values(all_directives, "error_log") != ["/dev/null crit"]:
        raise LogBoundaryError("宜老天气站点子块不得覆盖 error_log。")


def verify_nginx_site_access_log(config_path: Path) -> None:
    """读取离线配置并验证日志边界，供测试与人工预检使用。"""
    try:
        source = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LogBoundaryError("无法读取 Nginx 站点配置。") from error
    verify_nginx_source(source)


def verify_active_nginx() -> None:
    """执行 nginx -T，并验证完整活动配置文件集合。"""
    try:
        result = subprocess.run(
            ["/usr/sbin/nginx", "-T"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise LogBoundaryError("无法读取活动 Nginx 配置。") from error
    if result.returncode != 0:
        raise LogBoundaryError("活动 Nginx 配置语法校验失败。")
    verify_nginx_source(f"{result.stdout}\n{result.stderr}")


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--nginx-config", type=Path)
    source.add_argument("--active-nginx", action="store_true")
    args = parser.parse_args()
    try:
        if args.active_nginx:
            verify_active_nginx()
        else:
            verify_nginx_site_access_log(args.nginx_config)
    except LogBoundaryError as error:
        parser.error(str(error))
    print("runtime-log-boundary-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
