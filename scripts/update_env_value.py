#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从标准输入安全更新单个 dotenv 值，避免密钥进入远程命令行。"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def update_env_value(path: Path, key: str, value: str, mode: str) -> bool:
    """原子更新 dotenv；if-empty 模式会保留已有非空值。"""
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError("环境变量名不合法")
    if mode not in {"always", "if-empty"}:
        raise ValueError("更新模式不合法")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError("环境变量值不能包含换行或空字节")

    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()
    prefix = f"{key}="
    current_values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    current_value = current_values[-1] if current_values else None
    if mode == "if-empty" and current_value not in {None, ""}:
        return False

    kept_lines = [line for line in lines if not line.startswith(prefix)]
    kept_lines.append(f"{key}={value}")
    updated = "\n".join(kept_lines) + "\n"
    if updated == original:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(updated)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--key", required=True)
    parser.add_argument("--mode", choices=("always", "if-empty"), default="always")
    options = parser.parse_args(argv)
    value = os.sys.stdin.read()
    update_env_value(options.file, options.key, value, options.mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
