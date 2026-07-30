#!/usr/bin/env python3
"""为正式发布候选环境生成低内存运行态预检收据。"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import resource
import stat
import subprocess
import sys
import tempfile
from typing import Any, Sequence


# 主进程也禁止在候选代码目录生成字节码缓存。
sys.dont_write_bytecode = True

SCHEMA_VERSION = 1
RECEIPT_TYPE = "case-weather-release-runtime-smoke"
EXPECTED_PYTHON_MINOR = "3.11"
EXPECTED_REQUIREMENTS_LOCK_SHA256 = (
    "c7e450c30d7d3c56bdf210f69a58620cba9d99e462e0e2c254ab45456271f853"
)
MAX_RECEIPT_BYTES = 64 * 1024
HASH_CHUNK_BYTES = 64 * 1024
PIP_CHECK_TIMEOUT_SECONDS = 60

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PYTHON_MINOR_RE = re.compile(r"^[0-9]+\.[0-9]+$")
ALEMBIC_HEAD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
LOCK_REQUIREMENT_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s\\]+)(?:\s*\\)?$"
)

PACKAGE_IMPORTS = {
    "flask": ("flask", "Flask"),
    "sqlalchemy": ("sqlalchemy", "SQLAlchemy"),
    "alembic": ("alembic", "alembic"),
    "gunicorn": ("gunicorn", "gunicorn"),
}
CHECK_NAMES = {
    "alembic_single_head",
    "interpreter_path",
    "package_imports",
    "package_versions",
    "pip_check",
    "python_minor",
    "requirements_lock",
}


class SmokeError(RuntimeError):
    """表示发布预检或收据验收失败。"""


def _require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise SmokeError(f"{label} 必须是 JSON object。")
    actual = set(value)
    if actual != expected:
        raise SmokeError(f"{label} 字段集合不符合固定 schema。")
    return value


def _require_string(
    value: Any,
    label: str,
    *,
    max_length: int = 4096,
) -> str:
    if type(value) is not str or not value or len(value) > max_length:
        raise SmokeError(f"{label} 必须是非空且长度受限的字符串。")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SmokeError(f"{label} 不得包含控制字符。")
    return value


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _validate_fixed_expectations(
    expected_commit: str,
    expected_python_minor: str,
    expected_lock_sha: str,
) -> None:
    if not COMMIT_RE.fullmatch(expected_commit):
        raise SmokeError("expected commit 必须是 40 位小写十六进制 SHA。")
    if (
        not PYTHON_MINOR_RE.fullmatch(expected_python_minor)
        or expected_python_minor != EXPECTED_PYTHON_MINOR
    ):
        raise SmokeError(
            f"正式发布 Python minor 必须固定为 {EXPECTED_PYTHON_MINOR}。"
        )
    if (
        not SHA256_RE.fullmatch(expected_lock_sha)
        or expected_lock_sha != EXPECTED_REQUIREMENTS_LOCK_SHA256
    ):
        raise SmokeError("requirements.lock 摘要不符合正式发布固定基线。")


def _resolve_repo_root(value: str | os.PathLike[str]) -> Path:
    supplied = Path(value)
    if not supplied.is_absolute():
        raise SmokeError("repo root 必须使用绝对路径。")
    try:
        resolved = supplied.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise SmokeError("repo root 无法读取。") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise SmokeError("repo root 必须是目录。")
    return resolved


def _resolve_expected_python(
    value: str | os.PathLike[str],
) -> tuple[Path, Path, Path]:
    supplied = Path(value)
    if not supplied.is_absolute():
        raise SmokeError("expected python 必须使用绝对路径。")
    lexical = Path(os.path.abspath(os.fspath(supplied)))
    try:
        realpath = lexical.resolve(strict=True)
        metadata = realpath.stat()
        prefix = lexical.parent.parent.resolve(strict=True)
    except OSError as exc:
        raise SmokeError("expected python 无法读取。") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(realpath, os.X_OK):
        raise SmokeError("expected python 必须是可执行普通文件。")
    return lexical, realpath, prefix


def _stable_regular_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    require_mode: int | None = None,
) -> tuple[os.stat_result, bytes]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise SmokeError(f"无法读取文件: {path.name}") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise SmokeError(f"必须使用普通文件且禁止符号链接: {path.name}")
    if max_bytes is not None and before.st_size > max_bytes:
        raise SmokeError(f"文件超过大小限制: {path.name}")
    if require_mode is not None and stat.S_IMODE(before.st_mode) != require_mode:
        raise SmokeError(f"文件权限必须精确为 {require_mode:04o}: {path.name}")
    try:
        with path.open("rb") as handle:
            content = handle.read((max_bytes + 1) if max_bytes is not None else -1)
        after = path.lstat()
    except OSError as exc:
        raise SmokeError(f"读取文件失败: {path.name}") from exc
    if max_bytes is not None and len(content) > max_bytes:
        raise SmokeError(f"文件超过大小限制: {path.name}")
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise SmokeError(f"读取期间文件发生变化: {path.name}")
    return after, content


def _hash_regular_file(path: Path) -> str:
    try:
        before = path.lstat()
    except OSError as exc:
        raise SmokeError(f"无法读取文件: {path.name}") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise SmokeError(f"必须使用普通文件且禁止符号链接: {path.name}")

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        after = path.lstat()
    except OSError as exc:
        raise SmokeError(f"读取文件失败: {path.name}") from exc

    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise SmokeError(f"计算摘要期间文件发生变化: {path.name}")
    return digest.hexdigest()


def _locked_package_versions(lock_path: Path) -> dict[str, str]:
    wanted = {
        _normalize_distribution_name(distribution): key
        for key, (_, distribution) in PACKAGE_IMPORTS.items()
    }
    versions: dict[str, str] = {}
    try:
        with lock_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                match = LOCK_REQUIREMENT_RE.fullmatch(raw_line.strip())
                if match is None:
                    continue
                normalized = _normalize_distribution_name(match.group(1))
                key = wanted.get(normalized)
                if key is not None:
                    versions[key] = match.group(2)
    except (OSError, UnicodeDecodeError) as exc:
        raise SmokeError("requirements.lock 无法按 UTF-8 解析。") from exc
    if set(versions) != set(PACKAGE_IMPORTS):
        raise SmokeError("requirements.lock 缺少发布关键依赖的固定版本。")
    return versions


def _runtime_identity(expected_python: Path) -> dict[str, str]:
    expected_lexical, expected_realpath, expected_prefix = _resolve_expected_python(
        expected_python
    )
    actual_executable = Path(os.path.abspath(sys.executable))
    try:
        actual_realpath = actual_executable.resolve(strict=True)
        actual_prefix = Path(sys.prefix).resolve(strict=True)
    except OSError as exc:
        raise SmokeError("当前 Python 解释器路径无法验证。") from exc
    if (
        actual_executable != expected_lexical
        or actual_realpath != expected_realpath
        or actual_prefix != expected_prefix
    ):
        raise SmokeError("当前 Python 解释器与候选虚拟环境不一致。")

    actual_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_minor != EXPECTED_PYTHON_MINOR:
        raise SmokeError(
            f"当前 Python minor 为 {actual_minor}，正式基线要求 "
            f"{EXPECTED_PYTHON_MINOR}。"
        )
    return {
        "executable": os.fspath(actual_executable),
        "executable_realpath": os.fspath(actual_realpath),
        "minor": actual_minor,
        "prefix_realpath": os.fspath(actual_prefix),
        "version": platform.python_version(),
    }


def _run_pip_check() -> None:
    with tempfile.TemporaryDirectory(prefix="release-smoke-home.") as private_home:
        # 子进程只继承固定的非敏感变量，并强制关闭索引、配置文件与版本检查。
        child_env = {
            "HOME": private_home,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "XDG_CACHE_HOME": os.path.join(private_home, "cache"),
        }
        with tempfile.TemporaryFile() as output:
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-m", "pip", "check"],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=child_env,
                    timeout=PIP_CHECK_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SmokeError("pip check 超时。") from exc
            except OSError as exc:
                raise SmokeError("pip check 无法启动。") from exc
            if completed.returncode != 0:
                output.seek(0)
                summary = output.read(4096).decode("utf-8", errors="replace").strip()
                if summary:
                    raise SmokeError(f"pip check 未通过: {summary}")
                raise SmokeError("pip check 未通过。")


def _collect_packages(locked_versions: dict[str, str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for key, (module_name, distribution_name) in PACKAGE_IMPORTS.items():
        try:
            importlib.import_module(module_name)
            installed = importlib.metadata.version(distribution_name)
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            raise SmokeError(f"发布关键依赖无法导入: {key}") from exc
        if installed != locked_versions[key]:
            raise SmokeError(f"发布关键依赖版本与 requirements.lock 不一致: {key}")
        versions[key] = installed
    return versions


def _collect_alembic_heads(repo_root: Path) -> list[str]:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError as exc:
        raise SmokeError("Alembic 无法导入。") from exc

    config_path = repo_root / "alembic.ini"
    migrations_path = repo_root / "migrations"
    if not config_path.is_file() or not migrations_path.is_dir():
        raise SmokeError("Alembic 配置或 migrations 目录缺失。")
    try:
        config = Config(os.fspath(config_path))
        # 使用绝对路径，避免调用方工作目录影响迁移图解析。
        config.set_main_option("script_location", os.fspath(migrations_path))
        heads = list(ScriptDirectory.from_config(config).get_heads())
    except Exception as exc:
        raise SmokeError("Alembic 迁移图无法解析。") from exc
    if len(heads) != 1 or not ALEMBIC_HEAD_RE.fullmatch(heads[0]):
        raise SmokeError("Alembic 必须精确存在一个格式有效的 head。")
    return heads


def _normalized_ru_maxrss_kib() -> int:
    raw_values = [
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    ]
    raw = max(float(value) for value in raw_values)
    # macOS 报告字节，Linux 和其他目标 Unix 报告 KiB。
    if sys.platform == "darwin":
        return max(0, int((raw + 1023) // 1024))
    return max(0, int(raw))


def _utc_now_text() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute():
        raise SmokeError("output 必须使用绝对路径。")
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise SmokeError("output 父目录无法读取。") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(
        parent_metadata.st_mode
    ):
        raise SmokeError("output 父目录必须是普通目录且禁止符号链接。")
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SmokeError("output 目标状态无法验证。") from exc
    else:
        raise SmokeError("output 已存在，拒绝覆盖旧收据。")

    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise SmokeError("运行态预检收据超过大小限制。")

    descriptor = -1
    temporary_path: str | None = None
    linked = False
    completed = False
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # 同目录硬链接只在目标不存在时成功，形成原子且不可覆盖的发布。
        os.link(temporary_path, path)
        linked = True
        os.unlink(temporary_path)
        temporary_path = None
        final_metadata = path.lstat()
        if (
            not stat.S_ISREG(final_metadata.st_mode)
            or stat.S_IMODE(final_metadata.st_mode) != 0o600
        ):
            raise SmokeError("运行态预检收据权限验证失败。")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        completed = True
    except FileExistsError as exc:
        raise SmokeError("output 已存在，拒绝覆盖旧收据。") from exc
    except OSError as exc:
        raise SmokeError("无法原子写入运行态预检收据。") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        if linked and not completed:
            try:
                os.unlink(path)
            except OSError:
                pass


def create_receipt(
    *,
    repo_root: str | os.PathLike[str],
    expected_commit: str,
    expected_python: str | os.PathLike[str],
    expected_python_minor: str,
    expected_lock_sha: str,
    output: str | os.PathLike[str],
) -> dict[str, Any]:
    """执行在线候选环境预检，并只在全部通过后原子生成收据。"""

    _validate_fixed_expectations(
        expected_commit,
        expected_python_minor,
        expected_lock_sha,
    )
    root = _resolve_repo_root(repo_root)
    expected_python_path, _, _ = _resolve_expected_python(expected_python)
    output_path = Path(output)
    if not output_path.is_absolute():
        raise SmokeError("output 必须使用绝对路径。")
    if output_path.exists() or output_path.is_symlink():
        raise SmokeError("output 已存在，拒绝覆盖旧收据。")

    lock_path = root / "requirements.lock"
    actual_lock_sha = _hash_regular_file(lock_path)
    if actual_lock_sha != expected_lock_sha:
        raise SmokeError("requirements.lock 摘要与正式基线不一致。")
    locked_versions = _locked_package_versions(lock_path)

    python_identity = _runtime_identity(expected_python_path)
    _run_pip_check()
    packages = _collect_packages(locked_versions)
    heads = _collect_alembic_heads(root)

    receipt: dict[str, Any] = {
        "alembic": {"heads": heads},
        "checks": {name: True for name in sorted(CHECK_NAMES)},
        "commit_sha": expected_commit,
        "created_at_utc": _utc_now_text(),
        "packages": packages,
        "python": python_identity,
        "receipt_type": RECEIPT_TYPE,
        "requirements": {
            "lock_sha256": actual_lock_sha,
            "pip_check": "passed",
        },
        "resource": {"ru_maxrss_kib": _normalized_ru_maxrss_kib()},
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
    }
    _validate_receipt_schema(receipt)
    _atomic_write_private_json(output_path, receipt)
    return receipt


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SmokeError("收据 JSON 含重复字段。")
        result[key] = value
    return result


def _validate_created_at(value: Any) -> str:
    text = _require_string(value, "created_at_utc", max_length=32)
    if not text.endswith("Z"):
        raise SmokeError("created_at_utc 必须使用 UTC Z 格式。")
    try:
        parsed = dt.datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise SmokeError("created_at_utc 格式无效。") from exc
    if parsed.tzinfo != dt.timezone.utc:
        raise SmokeError("created_at_utc 必须使用 UTC。")
    return text


def _validate_receipt_schema(receipt: Any) -> dict[str, Any]:
    root = _require_exact_keys(
        receipt,
        {
            "alembic",
            "checks",
            "commit_sha",
            "created_at_utc",
            "packages",
            "python",
            "receipt_type",
            "requirements",
            "resource",
            "schema_version",
            "status",
        },
        "receipt",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != SCHEMA_VERSION:
        raise SmokeError("收据 schema_version 不受支持。")
    if root["receipt_type"] != RECEIPT_TYPE or root["status"] != "passed":
        raise SmokeError("收据类型或状态无效。")
    commit = _require_string(root["commit_sha"], "commit_sha", max_length=40)
    if not COMMIT_RE.fullmatch(commit):
        raise SmokeError("收据 commit_sha 格式无效。")
    _validate_created_at(root["created_at_utc"])

    python_data = _require_exact_keys(
        root["python"],
        {
            "executable",
            "executable_realpath",
            "minor",
            "prefix_realpath",
            "version",
        },
        "python",
    )
    for key in ("executable", "executable_realpath", "prefix_realpath"):
        path_text = _require_string(python_data[key], f"python.{key}")
        if not Path(path_text).is_absolute():
            raise SmokeError(f"python.{key} 必须是绝对路径。")
    minor = _require_string(python_data["minor"], "python.minor", max_length=16)
    version = _require_string(python_data["version"], "python.version", max_length=64)
    if not PYTHON_MINOR_RE.fullmatch(minor) or not (
        version == minor or version.startswith(minor + ".")
    ):
        raise SmokeError("收据 Python 版本字段不一致。")

    requirements = _require_exact_keys(
        root["requirements"],
        {"lock_sha256", "pip_check"},
        "requirements",
    )
    lock_sha = _require_string(
        requirements["lock_sha256"],
        "requirements.lock_sha256",
        max_length=64,
    )
    if not SHA256_RE.fullmatch(lock_sha) or requirements["pip_check"] != "passed":
        raise SmokeError("收据 requirements 字段无效。")

    packages = _require_exact_keys(root["packages"], set(PACKAGE_IMPORTS), "packages")
    for key, value in packages.items():
        _require_string(value, f"packages.{key}", max_length=128)

    alembic_data = _require_exact_keys(root["alembic"], {"heads"}, "alembic")
    heads = alembic_data["heads"]
    if (
        type(heads) is not list
        or len(heads) != 1
        or type(heads[0]) is not str
        or not ALEMBIC_HEAD_RE.fullmatch(heads[0])
    ):
        raise SmokeError("收据必须记录精确一个 Alembic head。")

    resource_data = _require_exact_keys(
        root["resource"],
        {"ru_maxrss_kib"},
        "resource",
    )
    rss = resource_data["ru_maxrss_kib"]
    if type(rss) is not int or rss < 0 or rss > 1_000_000_000:
        raise SmokeError("收据 ru_maxrss_kib 无效。")

    checks = _require_exact_keys(root["checks"], CHECK_NAMES, "checks")
    if any(value is not True for value in checks.values()):
        raise SmokeError("收据包含未通过的检查。")
    return root


def verify_receipt(
    *,
    receipt: str | os.PathLike[str],
    repo_root: str | os.PathLike[str],
    expected_commit: str,
    expected_python: str | os.PathLike[str],
    expected_python_minor: str,
    expected_lock_sha: str,
) -> dict[str, Any]:
    """离线验收既有收据，不导入应用、依赖包，也不执行子进程。"""

    _validate_fixed_expectations(
        expected_commit,
        expected_python_minor,
        expected_lock_sha,
    )
    root = _resolve_repo_root(repo_root)
    expected_lexical, expected_realpath, expected_prefix = _resolve_expected_python(
        expected_python
    )
    receipt_path = Path(receipt)
    if not receipt_path.is_absolute():
        raise SmokeError("receipt 必须使用绝对路径。")
    receipt_metadata, content = _stable_regular_file(
        receipt_path,
        max_bytes=MAX_RECEIPT_BYTES,
        require_mode=0o600,
    )
    if receipt_metadata.st_uid != os.geteuid() or receipt_metadata.st_nlink != 1:
        raise SmokeError("收据必须由当前激活用户独占，且禁止额外硬链接。")
    try:
        decoded = content.decode("utf-8")
        loaded = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except UnicodeDecodeError as exc:
        raise SmokeError("收据必须使用 UTF-8。") from exc
    except json.JSONDecodeError as exc:
        raise SmokeError("收据不是有效 JSON。") from exc
    validated = _validate_receipt_schema(loaded)

    if validated["commit_sha"] != expected_commit:
        raise SmokeError("收据 commit 与待激活提交不一致。")
    if validated["python"]["minor"] != expected_python_minor:
        raise SmokeError("收据 Python minor 与正式基线不一致。")
    if validated["requirements"]["lock_sha256"] != expected_lock_sha:
        raise SmokeError("收据 lock 摘要与正式基线不一致。")

    actual_lock_sha = _hash_regular_file(root / "requirements.lock")
    if actual_lock_sha != expected_lock_sha:
        raise SmokeError("当前 release 的 requirements.lock 摘要不一致。")
    locked_versions = _locked_package_versions(root / "requirements.lock")
    if validated["packages"] != locked_versions:
        raise SmokeError("收据关键依赖版本与当前 requirements.lock 不一致。")

    receipt_executable = Path(validated["python"]["executable"])
    try:
        receipt_realpath = receipt_executable.resolve(strict=True)
    except OSError as exc:
        raise SmokeError("收据记录的 Python 解释器已不可用。") from exc
    if (
        Path(os.path.abspath(receipt_executable)) != expected_lexical
        or receipt_realpath != expected_realpath
        or Path(validated["python"]["executable_realpath"]) != expected_realpath
        or Path(validated["python"]["prefix_realpath"]) != expected_prefix
        or expected_lexical.parent.parent.resolve(strict=True) != expected_prefix
    ):
        raise SmokeError("收据 Python 路径与候选虚拟环境不一致。")
    return validated


def _add_expectation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-python", required=True)
    parser.add_argument("--expected-python-minor", required=True)
    parser.add_argument("--expected-lock-sha", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="执行或离线验收正式发布低内存运行态预检。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="执行候选环境预检并生成收据。")
    _add_expectation_arguments(run_parser)
    run_parser.add_argument("--output", required=True)

    verify_parser = subparsers.add_parser(
        "verify-receipt",
        help="离线验收既有预检收据。",
    )
    _add_expectation_arguments(verify_parser)
    verify_parser.add_argument("--receipt", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            create_receipt(
                repo_root=args.repo_root,
                expected_commit=args.expected_commit,
                expected_python=args.expected_python,
                expected_python_minor=args.expected_python_minor,
                expected_lock_sha=args.expected_lock_sha,
                output=args.output,
            )
            print("OK: release runtime smoke receipt created")
            return 0
        verify_receipt(
            receipt=args.receipt,
            repo_root=args.repo_root,
            expected_commit=args.expected_commit,
            expected_python=args.expected_python,
            expected_python_minor=args.expected_python_minor,
            expected_lock_sha=args.expected_lock_sha,
        )
        print("OK: release runtime smoke receipt verified")
        return 0
    except SmokeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
