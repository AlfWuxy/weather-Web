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


DEPLOY_KEYS = ("QWEATHER_KEY", "QWEATHER_API_BASE", "AMAP_KEY", "WXPUSHER_APP_TOKEN", "PUBLIC_BASE_URL")


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
        json.dump({key: os.environ.get(key, values.get(key, "")) for key in DEPLOY_KEYS}, sys.stdout)
    elif args.mode == "merge":
        updates = json.load(sys.stdin)
        if set(updates) != set(DEPLOY_KEYS):
            raise ValueError("部署配置字段不匹配")
        _, current = read_values(args.path, create=True)
        merged = {key: value for key, value in updates.items() if value and not current.get(key)}
        merged["PUBLIC_BASE_URL"] = updates["PUBLIC_BASE_URL"]
        merged["ALLOW_INSECURE_PUBLIC_BASE_URL"] = "0"
        write_values(args.path, merged)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError) as exc:
        print(f"环境文件操作失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
