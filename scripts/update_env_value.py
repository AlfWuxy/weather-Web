#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从标准输入安全更新单个 dotenv 值，避免密钥进入远程命令行。"""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
MAX_ENV_VALUE_BYTES = 64 * 1024


@dataclass(frozen=True)
class _EnvSnapshot:
    content: str
    fingerprint: tuple | None
    mode: int | None


def _file_fingerprint(file_stat):
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        file_stat.st_mode,
    )


def _read_existing_env(path: Path) -> _EnvSnapshot:
    """只从未变化的普通文件读取现有配置，拒绝链接和特殊文件。"""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return _EnvSnapshot("", None, None)
    if not stat.S_ISREG(path_stat.st_mode):
        raise ValueError("目标环境文件必须是普通文件，不能使用符号链接")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("目标环境文件无法安全读取") from error

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("目标环境文件必须是普通文件，不能使用符号链接")
        if (before.st_dev, before.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise ValueError("目标环境文件读取前发生变化")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            content = source.read()
            after = os.fstat(source.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        current_path_stat = path.lstat()
    except FileNotFoundError as error:
        raise ValueError("目标环境文件读取期间发生变化") from error
    if (
        _file_fingerprint(before) != _file_fingerprint(after)
        or _file_fingerprint(current_path_stat) != _file_fingerprint(after)
    ):
        raise ValueError("目标环境文件读取期间发生变化")
    try:
        decoded_content = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("目标环境文件必须是 UTF-8 文本") from None
    return _EnvSnapshot(
        decoded_content,
        _file_fingerprint(after),
        stat.S_IMODE(after.st_mode),
    )


def _atomic_replace_if_unchanged(
    path: Path,
    expected: _EnvSnapshot,
    updated: str,
) -> None:
    """仅在目标仍与已读快照一致时执行 0600 原子替换。"""
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
            os.fchmod(target.fileno(), 0o600)
            os.fsync(target.fileno())
        if _read_existing_env(path) != expected:
            raise ValueError("目标环境文件更新期间发生变化")
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _finish_noop_safely(path: Path, snapshot: _EnvSnapshot) -> bool:
    """无内容变化时仍保证已有文件权限严格为 0600。"""
    if snapshot.fingerprint is not None and snapshot.mode != 0o600:
        _atomic_replace_if_unchanged(path, snapshot, snapshot.content)
    return False


def _read_stdin_value() -> str:
    """从标准输入最多读取上限加一字节，以便明确拒绝超长输入。"""
    content = os.sys.stdin.buffer.read(MAX_ENV_VALUE_BYTES + 1)
    if len(content) > MAX_ENV_VALUE_BYTES:
        raise ValueError("环境变量值过长")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("环境变量值必须是 UTF-8 文本") from None


def update_env_value(path: Path, key: str, value: str, mode: str) -> bool:
    """原子更新 dotenv；if-empty 模式会保留已有非空值。"""
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError("环境变量名不合法")
    if mode not in {"always", "if-empty"}:
        raise ValueError("更新模式不合法")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError("环境变量值不能包含换行或空字节")
    try:
        value_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("环境变量值必须是 UTF-8 文本") from None
    if value_size > MAX_ENV_VALUE_BYTES:
        raise ValueError("环境变量值过长")

    original_snapshot = _read_existing_env(path)
    original = original_snapshot.content
    lines = original.splitlines()
    prefix = f"{key}="
    current_values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    current_value = current_values[-1] if current_values else None
    if mode == "if-empty" and current_value not in {None, ""}:
        return _finish_noop_safely(path, original_snapshot)

    kept_lines = [line for line in lines if not line.startswith(prefix)]
    kept_lines.append(f"{key}={value}")
    updated = "\n".join(kept_lines) + "\n"
    if updated == original:
        return _finish_noop_safely(path, original_snapshot)

    _atomic_replace_if_unchanged(path, original_snapshot, updated)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--key", required=True)
    parser.add_argument("--mode", choices=("always", "if-empty"), default="always")
    options = parser.parse_args(argv)
    try:
        value = _read_stdin_value()
        update_env_value(options.file, options.key, value, options.mode)
    except (OSError, ValueError) as error:
        parser.exit(1, f"错误：{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
