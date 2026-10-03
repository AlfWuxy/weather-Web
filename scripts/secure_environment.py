#!/usr/bin/env python3
"""维护入口复用发布器的快照校验与私有原子写入，不传递秘密参数。"""
import argparse
import os
from pathlib import Path
import re
import secrets
import stat
import sys

from update_env_value import _atomic_replace_if_unchanged, _read_existing_env


def protect_file(path):
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("环境文件必须是非链接普通文件")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def line_key(line):
    key = line.partition("=")[0].strip()
    return key.removeprefix("export ").strip()


def update_values(path, updates):
    protect_file(path)
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or any(char in value for char in "\n\r\x00"):
            raise ValueError("环境变量名或值无效")
    snapshot = _read_existing_env(path)
    # 合并空白键名、export形式及重复旧键，保留其他配置字节。
    kept = [line for line in snapshot.content.splitlines(keepends=True) if line_key(line) not in updates]
    if kept and not kept[-1].endswith(("\r", "\n")):
        kept[-1] += "\n"
    updated = "".join(kept) + "".join(f"{key}={value}\n" for key, value in updates.items())
    if updated != snapshot.content:
        _atomic_replace_if_unchanged(path, snapshot, updated)


def ensure_defaults(path):
    protect_file(path)
    snapshot = _read_existing_env(path)
    values = {}
    for line in snapshot.content.splitlines():
        key = line_key(line)
        if key not in ("SECRET_KEY", "PAIR_TOKEN_PEPPER") or "=" not in line:
            continue
        value = line.partition("=")[2].strip()
        if value.startswith(('"', "'")):
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    update_values(path, {key: secrets.token_hex(32) for key in ("SECRET_KEY", "PAIR_TOKEN_PEPPER") if not values.get(key)})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("protect", "set", "defaults"))
    parser.add_argument("path", type=Path)
    parser.add_argument("key", nargs="?")
    args = parser.parse_args()
    if args.mode == "protect": protect_file(args.path)
    elif args.mode == "defaults": ensure_defaults(args.path)
    else:
        value = sys.stdin.read(65537)
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("环境变量值过长")
        update_values(args.path, {args.key: value})


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError) as exc:
        print(f"环境文件维护失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
