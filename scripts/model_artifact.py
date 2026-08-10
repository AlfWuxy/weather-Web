#!/usr/bin/env python3
"""安全冻结并校验正式运行所需的模型制品。"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
from typing import Any, Sequence


sys.dont_write_bytecode = True

ARTIFACT_NAMES = (
    "disease_predictor.pkl",
    "scaler.pkl",
    "label_encoder.pkl",
)
EXPECTED_SKLEARN_VERSION = "1.7.2"
SCHEMA_VERSION = 1
RECEIPT_TYPE = "yilao-model-artifact-snapshot"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NUMERIC_ID_RE = re.compile(r"^[0-9]{1,10}$")
MODE_RE = re.compile(r"^0[0-7]{3}$")
MAX_NUMERIC_ID = 2**32 - 2


class ModelArtifactError(RuntimeError):
    """模型制品不满足固定发布契约。"""


def _required_open_flags(*, directory: bool = False) -> int:
    """所有受信文件都必须由内核拒绝尾部符号链接并关闭继承。"""
    required = ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
    if directory:
        required += ("O_DIRECTORY",)
    if any(not hasattr(os, name) for name in required):
        raise ModelArtifactError("当前系统缺少安全文件打开能力。")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_absolute_path(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ModelArtifactError(f"{label} 必须使用绝对路径。")
    return Path(os.path.abspath(os.fspath(path)))


def _open_directory(
    path: Path,
    *,
    require_absolute: bool,
    label: str,
) -> tuple[int, os.stat_result]:
    if require_absolute and not path.is_absolute():
        raise ModelArtifactError(f"{label} 必须使用绝对路径。")
    descriptor: int | None = None
    try:
        lexical_metadata = path.lstat()
        if stat.S_ISLNK(lexical_metadata.st_mode) or not stat.S_ISDIR(
            lexical_metadata.st_mode
        ):
            raise ModelArtifactError(f"{label} 必须是非符号链接目录。")
        descriptor = os.open(path, _required_open_flags(directory=True))
        opened_metadata = os.fstat(descriptor)
    except ModelArtifactError:
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise ModelArtifactError(f"{label} 无法安全打开。") from error

    if (
        not stat.S_ISDIR(opened_metadata.st_mode)
        or _fingerprint(lexical_metadata) != _fingerprint(opened_metadata)
    ):
        os.close(descriptor)
        raise ModelArtifactError(f"{label} 在打开期间发生变化。")
    return descriptor, opened_metadata


def _read_descriptor_stably(
    descriptor: int,
    *,
    before: os.stat_result,
    maximum: int,
    label: str,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, min(COPY_CHUNK_BYTES, maximum + 1 - total))
        except OSError as error:
            raise ModelArtifactError(f"{label} 无法安全读取。") from error
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ModelArtifactError(f"{label} 超过大小上限。")
    try:
        after = os.fstat(descriptor)
    except OSError as error:
        raise ModelArtifactError(f"{label} 无法安全读取。") from error
    if _fingerprint(before) != _fingerprint(after):
        raise ModelArtifactError(f"{label} 在读取期间发生变化。")
    return b"".join(chunks)


def _read_json_file(
    path: Path,
    *,
    maximum: int,
    required_mode: int | None = None,
    require_owner: bool = False,
    require_single_link: bool = False,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    try:
        lexical = path.lstat()
        if stat.S_ISLNK(lexical.st_mode) or not stat.S_ISREG(lexical.st_mode):
            raise ModelArtifactError(f"{label} 必须是普通文件。")
        descriptor = os.open(path, _required_open_flags())
    except ModelArtifactError:
        raise
    except OSError as error:
        raise ModelArtifactError(f"{label} 无法安全打开。") from error

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _fingerprint(lexical) != _fingerprint(opened)
            or opened.st_size > maximum
        ):
            raise ModelArtifactError(f"{label} 元数据不符合要求。")
        if required_mode is not None and stat.S_IMODE(opened.st_mode) != required_mode:
            raise ModelArtifactError(f"{label} 权限必须为 {required_mode:04o}。")
        if require_owner and opened.st_uid != os.geteuid():
            raise ModelArtifactError(f"{label} 所有者不符合要求。")
        if require_single_link and opened.st_nlink != 1:
            raise ModelArtifactError(f"{label} 硬链接数量不符合要求。")
        content = _read_descriptor_stably(
            descriptor,
            before=opened,
            maximum=maximum,
            label=label,
        )
    finally:
        os.close(descriptor)

    try:
        final = path.lstat()
    except OSError as error:
        raise ModelArtifactError(f"{label} 在读取期间发生变化。") from error
    if _fingerprint(opened) != _fingerprint(final):
        raise ModelArtifactError(f"{label} 在读取期间发生变化。")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ModelArtifactError(f"{label} 包含重复字段。")
            result[key] = value
        return result

    try:
        parsed = json.loads(content, object_pairs_hook=reject_duplicate_keys)
    except ModelArtifactError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise ModelArtifactError(f"{label} 必须是有效 UTF-8 JSON。") from error
    if type(parsed) is not dict:
        raise ModelArtifactError(f"{label} 根节点必须是 JSON object。")
    return parsed, content


def _require_exact_object(
    value: Any,
    *,
    keys: set[str],
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ModelArtifactError(f"{label} 字段集合不符合固定 schema。")
    return value


def _validate_file_spec(value: Any, filename: str) -> dict[str, Any]:
    item = _require_exact_object(
        value,
        keys={"sha256", "size_bytes"},
        label=f"{filename} manifest",
    )
    digest = item["sha256"]
    size = item["size_bytes"]
    if type(digest) is not str or not SHA256_RE.fullmatch(digest):
        raise ModelArtifactError(f"{filename} 摘要格式异常。")
    if type(size) is not int or not (0 < size <= MAX_ARTIFACT_BYTES):
        raise ModelArtifactError(f"{filename} 大小格式异常。")
    return {"sha256": digest, "size_bytes": size}


def load_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    manifest_path = Path(path)
    parsed, _ = _read_json_file(
        manifest_path,
        maximum=MAX_MANIFEST_BYTES,
        label="模型 manifest",
    )
    runtime = _require_exact_object(
        parsed.get("runtime_artifacts"),
        keys={"expected_sklearn_version", "files"},
        label="runtime_artifacts",
    )
    if runtime["expected_sklearn_version"] != EXPECTED_SKLEARN_VERSION:
        raise ModelArtifactError(
            f"expected_sklearn_version 必须固定为 {EXPECTED_SKLEARN_VERSION}。"
        )
    files = _require_exact_object(
        runtime["files"],
        keys=set(ARTIFACT_NAMES),
        label="runtime_artifacts.files",
    )
    return {
        "expected_sklearn_version": EXPECTED_SKLEARN_VERSION,
        "files": {
            filename: _validate_file_spec(files[filename], filename)
            for filename in ARTIFACT_NAMES
        },
    }


def _validate_private_artifact_metadata(
    metadata: os.stat_result,
    filename: str,
    *,
    label: str,
    expected_uid: int,
    expected_gid: int | None,
    expected_mode: int,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or (
            expected_gid is not None
            and metadata.st_gid != expected_gid
        )
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or metadata.st_nlink != 1
    ):
        raise ModelArtifactError(f"{filename} {label}元数据不符合要求。")


def _open_source_file(
    source_directory_fd: int,
    filename: str,
) -> tuple[int, os.stat_result]:
    descriptor: int | None = None
    try:
        before = os.stat(
            filename,
            dir_fd=source_directory_fd,
            follow_symlinks=False,
        )
        _validate_private_artifact_metadata(
            before,
            filename,
            label="源文件",
            expected_uid=os.geteuid(),
            expected_gid=None,
            expected_mode=0o600,
        )
        descriptor = os.open(
            filename,
            _required_open_flags(),
            dir_fd=source_directory_fd,
        )
        opened = os.fstat(descriptor)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise ModelArtifactError(
            f"{filename} 源文件无法安全打开。"
        ) from error
    try:
        _validate_private_artifact_metadata(
            before,
            filename,
            label="源文件",
            expected_uid=os.geteuid(),
            expected_gid=None,
            expected_mode=0o600,
        )
        _validate_private_artifact_metadata(
            opened,
            filename,
            label="源文件",
            expected_uid=os.geteuid(),
            expected_gid=None,
            expected_mode=0o600,
        )
        if _fingerprint(before) != _fingerprint(opened):
            raise ModelArtifactError(
                f"{filename} 源文件在打开期间发生变化。"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _require_exact_pickle_entries(
    directory_fd: int,
    *,
    label: str,
) -> None:
    """稳定枚举目录，并拒绝固定三项以外的任何 pkl 条目。"""
    try:
        before = os.fstat(directory_fd)
        names = os.listdir(directory_fd)
        after = os.fstat(directory_fd)
    except OSError as error:
        raise ModelArtifactError(f"{label}无法安全列出。") from error
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(after.st_mode)
        or _fingerprint(before) != _fingerprint(after)
    ):
        raise ModelArtifactError(f"{label}在列出期间发生变化。")
    pickle_names = {name for name in names if name.endswith(".pkl")}
    if pickle_names != set(ARTIFACT_NAMES):
        raise ModelArtifactError(f"{label}中的 pkl 条目集合不符合要求。")


def _preflight_source_directory(source_directory_fd: int) -> None:
    """复制前安全确认源目录集合与三项普通文件边界。"""
    _require_exact_pickle_entries(
        source_directory_fd,
        label="模型源目录",
    )
    for filename in ARTIFACT_NAMES:
        descriptor, _ = _open_source_file(source_directory_fd, filename)
        os.close(descriptor)
    _require_exact_pickle_entries(
        source_directory_fd,
        label="模型源目录",
    )


def _create_output_directory(path: Path) -> tuple[int, int, str]:
    if path.parent == path or path.name in {"", ".", ".."}:
        raise ModelArtifactError("输出目录路径无效。")
    parent_fd, _ = _open_directory(
        path.parent,
        require_absolute=True,
        label="输出目录父目录",
    )
    created = False
    directory_fd: int | None = None
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        created = True
        directory_fd = os.open(
            path.name,
            _required_open_flags(directory=True),
            dir_fd=parent_fd,
        )
        os.fchmod(directory_fd, 0o700)
        metadata = os.fstat(directory_fd)
        path_metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or _fingerprint(metadata) != _fingerprint(path_metadata)
        ):
            raise ModelArtifactError("输出目录元数据不符合要求。")
        os.fsync(parent_fd)
        return parent_fd, directory_fd, path.name
    except FileExistsError as error:
        os.close(parent_fd)
        raise ModelArtifactError("输出目录必须是全新目录。") from error
    except ModelArtifactError:
        if directory_fd is not None:
            os.close(directory_fd)
        if created:
            try:
                os.rmdir(path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
        raise
    except OSError as error:
        if directory_fd is not None:
            os.close(directory_fd)
        if created:
            try:
                os.rmdir(path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
        raise ModelArtifactError("输出目录无法安全创建。") from error


def _remove_partial_output(
    *,
    parent_fd: int,
    directory_fd: int,
    directory_name: str,
) -> None:
    for filename in ARTIFACT_NAMES:
        try:
            os.unlink(filename, dir_fd=directory_fd)
        except FileNotFoundError:
            continue
        except OSError:
            continue
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    try:
        os.rmdir(directory_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError:
        pass


def _copy_one_artifact(
    *,
    source_directory_fd: int,
    output_directory_fd: int,
    filename: str,
    expected: dict[str, Any],
) -> None:
    source_fd, source_before = _open_source_file(source_directory_fd, filename)
    output_fd: int | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        if source_before.st_size != expected["size_bytes"]:
            raise ModelArtifactError(
                f"{filename} 源文件大小与 manifest 不一致。"
            )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
        )
        output_fd = os.open(
            filename,
            flags,
            0o600,
            dir_fd=output_directory_fd,
        )
        os.fchmod(output_fd, 0o600)
        while True:
            chunk = os.read(source_fd, COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > expected["size_bytes"]:
                raise ModelArtifactError(
                    f"{filename} 源文件大小与 manifest 不一致。"
                )
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                if written <= 0:
                    raise ModelArtifactError(f"{filename} 输出写入失败。")
                view = view[written:]
        os.fsync(output_fd)

        source_after = os.fstat(source_fd)
        source_entry_after = os.stat(
            filename,
            dir_fd=source_directory_fd,
            follow_symlinks=False,
        )
        if not (
            _fingerprint(source_before)
            == _fingerprint(source_after)
            == _fingerprint(source_entry_after)
        ):
            raise ModelArtifactError(
                f"{filename} 源文件在读取期间发生变化。"
            )
        if total != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
            raise ModelArtifactError(f"{filename} 内容与 manifest 不一致。")

        output_metadata = os.fstat(output_fd)
        output_entry = os.stat(
            filename,
            dir_fd=output_directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(output_metadata.st_mode)
            or output_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(output_metadata.st_mode) != 0o600
            or output_metadata.st_nlink != 1
            or output_metadata.st_size != expected["size_bytes"]
            or _fingerprint(output_metadata) != _fingerprint(output_entry)
        ):
            raise ModelArtifactError(f"{filename} 输出元数据不符合要求。")
    except OSError as error:
        raise ModelArtifactError(f"{filename} 快照失败。") from error
    finally:
        if output_fd is not None:
            os.close(output_fd)
        os.close(source_fd)


def _hash_artifact_from_directory(
    directory_fd: int,
    filename: str,
    expected: dict[str, Any],
    *,
    expected_uid: int,
    expected_gid: int | None,
    expected_mode: int,
) -> None:
    descriptor: int | None = None
    try:
        before = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        _validate_private_artifact_metadata(
            before,
            filename,
            label="",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_mode=expected_mode,
        )
        descriptor = os.open(
            filename,
            _required_open_flags(),
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise ModelArtifactError(f"{filename} 无法安全校验。") from error

    digest = hashlib.sha256()
    total = 0
    try:
        _validate_private_artifact_metadata(
            before,
            filename,
            label="",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_mode=expected_mode,
        )
        _validate_private_artifact_metadata(
            opened,
            filename,
            label="",
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_mode=expected_mode,
        )
        if (
            _fingerprint(before) != _fingerprint(opened)
            or opened.st_size != expected["size_bytes"]
        ):
            raise ModelArtifactError(f"{filename} 元数据与 manifest 不一致。")
        while True:
            chunk = os.read(descriptor, COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > expected["size_bytes"]:
                raise ModelArtifactError(f"{filename} 大小与 manifest 不一致。")
        finished = os.fstat(descriptor)
        entry_after = os.stat(
            filename,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ModelArtifactError(f"{filename} 无法安全校验。") from error
    finally:
        os.close(descriptor)

    if not (
        _fingerprint(before)
        == _fingerprint(opened)
        == _fingerprint(finished)
        == _fingerprint(entry_after)
    ):
        raise ModelArtifactError(f"{filename} 在校验期间发生变化。")
    if total != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
        raise ModelArtifactError(f"{filename} 内容与 manifest 不一致。")


def _verify_open_artifact_directory(
    directory_fd: int,
    manifest: dict[str, Any],
    *,
    expected_uid: int,
    expected_gid: int | None,
    expected_file_mode: int,
) -> None:
    _require_exact_pickle_entries(
        directory_fd,
        label="模型制品目录",
    )
    for filename in ARTIFACT_NAMES:
        _hash_artifact_from_directory(
            directory_fd,
            filename,
            manifest["files"][filename],
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_mode=expected_file_mode,
        )


def _receipt_payload(
    manifest: dict[str, Any],
    *,
    commit: str,
) -> dict[str, Any]:
    _validate_commit(commit)
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_type": RECEIPT_TYPE,
        "status": "passed",
        "commit": commit,
        "expected_sklearn_version": EXPECTED_SKLEARN_VERSION,
        "files": {
            filename: dict(manifest["files"][filename])
            for filename in ARTIFACT_NAMES
        },
    }


def _validate_commit(commit: Any) -> str:
    if type(commit) is not str or not COMMIT_RE.fullmatch(commit):
        raise ModelArtifactError("commit 必须是 40 位小写十六进制 SHA。")
    return commit


def _resolve_expected_owner(value: str | None) -> int:
    if value is None:
        return os.geteuid()
    if type(value) is not str or not value:
        raise ModelArtifactError("expected-owner 格式异常。")
    if NUMERIC_ID_RE.fullmatch(value):
        uid = int(value, 10)
        if uid > MAX_NUMERIC_ID:
            raise ModelArtifactError("expected-owner 格式异常。")
        return uid
    try:
        uid = pwd.getpwnam(value).pw_uid
    except (KeyError, OSError, OverflowError) as error:
        raise ModelArtifactError("expected-owner 无法解析。") from error
    if not (0 <= uid <= MAX_NUMERIC_ID):
        raise ModelArtifactError("expected-owner 无法解析。")
    return uid


def _resolve_expected_group(value: str | None) -> int | None:
    if value is None:
        return None
    if type(value) is not str or not value:
        raise ModelArtifactError("expected-group 格式异常。")
    if NUMERIC_ID_RE.fullmatch(value):
        gid = int(value, 10)
        if gid > MAX_NUMERIC_ID:
            raise ModelArtifactError("expected-group 格式异常。")
        return gid
    try:
        gid = grp.getgrnam(value).gr_gid
    except (KeyError, OSError, OverflowError) as error:
        raise ModelArtifactError("expected-group 无法解析。") from error
    if not (0 <= gid <= MAX_NUMERIC_ID):
        raise ModelArtifactError("expected-group 无法解析。")
    return gid


def _parse_expected_mode(value: str, *, label: str) -> int:
    if type(value) is not str or not MODE_RE.fullmatch(value):
        raise ModelArtifactError(f"{label} 必须是四位八进制权限。")
    return int(value, 8)


def _write_new_private_json(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute() or path.parent == path or path.name in {"", ".", ".."}:
        raise ModelArtifactError("receipt 必须使用有效绝对路径。")
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(serialized) > MAX_RECEIPT_BYTES:
        raise ModelArtifactError("receipt 超过大小上限。")

    parent_fd, _ = _open_directory(
        path.parent,
        require_absolute=True,
        label="receipt 父目录",
    )
    descriptor: int | None = None
    created = False
    completed = False
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
        )
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        created = True
        os.fchmod(descriptor, 0o600)
        view = memoryview(serialized)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ModelArtifactError("receipt 写入失败。")
            view = view[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size != len(serialized)
            or _fingerprint(metadata) != _fingerprint(entry)
        ):
            raise ModelArtifactError("receipt 元数据不符合要求。")
        os.fsync(parent_fd)
        completed = True
    except FileExistsError as error:
        raise ModelArtifactError("receipt 必须是全新文件。") from error
    except OSError as error:
        raise ModelArtifactError("receipt 无法安全写入。") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created and not completed:
            try:
                os.unlink(path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def _validate_receipt(
    path: Path,
    manifest: dict[str, Any],
    *,
    expected_commit: str,
) -> None:
    parsed, _ = _read_json_file(
        path,
        maximum=MAX_RECEIPT_BYTES,
        required_mode=0o600,
        require_owner=True,
        require_single_link=True,
        label="模型制品 receipt",
    )
    receipt = _require_exact_object(
        parsed,
        keys={
            "schema_version",
            "receipt_type",
            "status",
            "commit",
            "expected_sklearn_version",
            "files",
        },
        label="模型制品 receipt",
    )
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != SCHEMA_VERSION
        or receipt["receipt_type"] != RECEIPT_TYPE
        or receipt["status"] != "passed"
        or receipt["expected_sklearn_version"] != EXPECTED_SKLEARN_VERSION
        or type(receipt["commit"]) is not str
        or not COMMIT_RE.fullmatch(receipt["commit"])
    ):
        raise ModelArtifactError("模型制品 receipt 固定字段不符合要求。")
    receipt_files = _require_exact_object(
        receipt["files"],
        keys=set(ARTIFACT_NAMES),
        label="模型制品 receipt.files",
    )
    normalized = {
        filename: _validate_file_spec(receipt_files[filename], filename)
        for filename in ARTIFACT_NAMES
    }
    if normalized != manifest["files"]:
        raise ModelArtifactError("模型制品 receipt 与 manifest 不一致。")
    if receipt["commit"] != expected_commit:
        raise ModelArtifactError("模型制品 receipt 与本轮 commit 不一致。")


def snapshot(
    *,
    source_dir: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    receipt_path: str | os.PathLike[str],
    commit: str,
) -> dict[str, Any]:
    source_path = _require_absolute_path(source_dir, "source-dir")
    output_path = _require_absolute_path(output_dir, "output-dir")
    receipt = _require_absolute_path(receipt_path, "receipt")
    manifest = load_manifest(manifest_path)
    payload = _receipt_payload(manifest, commit=commit)

    source_fd, _ = _open_directory(
        source_path,
        require_absolute=True,
        label="source-dir",
    )
    parent_fd: int | None = None
    output_fd: int | None = None
    output_name = ""
    try:
        _preflight_source_directory(source_fd)
        parent_fd, output_fd, output_name = _create_output_directory(output_path)
        try:
            for filename in ARTIFACT_NAMES:
                _copy_one_artifact(
                    source_directory_fd=source_fd,
                    output_directory_fd=output_fd,
                    filename=filename,
                    expected=manifest["files"][filename],
                )
            os.fsync(output_fd)
            _verify_open_artifact_directory(
                output_fd,
                manifest,
                expected_uid=os.geteuid(),
                expected_gid=None,
                expected_file_mode=0o600,
            )
            _preflight_source_directory(source_fd)
            _write_new_private_json(receipt, payload)
        except Exception:
            _remove_partial_output(
                parent_fd=parent_fd,
                directory_fd=output_fd,
                directory_name=output_name,
            )
            raise
    finally:
        if output_fd is not None:
            os.close(output_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(source_fd)
    return payload


def verify(
    *,
    artifact_dir: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
    receipt_path: str | os.PathLike[str] | None = None,
    commit: str | None = None,
    expected_owner: str | None = None,
    expected_group: str | None = None,
    expected_file_mode: str = "0600",
    expected_dir_mode: str = "0700",
) -> None:
    if receipt_path is not None and commit is None:
        raise ModelArtifactError("校验 receipt 时必须同时提供 commit。")
    if receipt_path is None and commit is not None:
        raise ModelArtifactError("commit 只能与 receipt 一起校验。")
    expected_commit = _validate_commit(commit) if commit is not None else None
    expected_uid = _resolve_expected_owner(expected_owner)
    expected_gid = _resolve_expected_group(expected_group)
    file_mode = _parse_expected_mode(
        expected_file_mode,
        label="expected-file-mode",
    )
    directory_mode = _parse_expected_mode(
        expected_dir_mode,
        label="expected-dir-mode",
    )
    artifact_path = _require_absolute_path(artifact_dir, "artifact-dir")
    manifest = load_manifest(manifest_path)
    directory_fd, directory_before = _open_directory(
        artifact_path,
        require_absolute=True,
        label="artifact-dir",
    )
    try:
        if (
            directory_before.st_uid != expected_uid
            or (
                expected_gid is not None
                and directory_before.st_gid != expected_gid
            )
            or stat.S_IMODE(directory_before.st_mode) != directory_mode
        ):
            raise ModelArtifactError("artifact-dir 元数据不符合要求。")
        _verify_open_artifact_directory(
            directory_fd,
            manifest,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            expected_file_mode=file_mode,
        )
        directory_after = os.fstat(directory_fd)
        if _fingerprint(directory_before) != _fingerprint(directory_after):
            raise ModelArtifactError("artifact-dir 在校验期间发生变化。")
    finally:
        os.close(directory_fd)
    if receipt_path is not None:
        receipt = _require_absolute_path(receipt_path, "receipt")
        if expected_commit is None:
            raise ModelArtifactError("校验 receipt 时必须同时提供 commit。")
        _validate_receipt(
            receipt,
            manifest,
            expected_commit=expected_commit,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结或验证宜老天气通的固定模型制品。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--source-dir", required=True)
    snapshot_parser.add_argument("--manifest", required=True)
    snapshot_parser.add_argument("--output-dir", required=True)
    snapshot_parser.add_argument("--receipt", required=True)
    snapshot_parser.add_argument("--commit", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--artifact-dir", required=True)
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--receipt")
    verify_parser.add_argument("--commit")
    verify_parser.add_argument("--expected-owner")
    verify_parser.add_argument("--expected-group")
    verify_parser.add_argument("--expected-file-mode", default="0600")
    verify_parser.add_argument("--expected-dir-mode", default="0700")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            snapshot(
                source_dir=args.source_dir,
                manifest_path=args.manifest,
                output_dir=args.output_dir,
                receipt_path=args.receipt,
                commit=args.commit,
            )
            print("OK: 模型制品快照已创建")
        else:
            verify(
                artifact_dir=args.artifact_dir,
                manifest_path=args.manifest,
                receipt_path=args.receipt,
                commit=args.commit,
                expected_owner=args.expected_owner,
                expected_group=args.expected_group,
                expected_file_mode=args.expected_file_mode,
                expected_dir_mode=args.expected_dir_mode,
            )
            print("OK: 模型制品校验通过")
    except ModelArtifactError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except Exception:
        # 未预期异常也必须失败关闭，禁止把本机路径或文件内容写入日志。
        print("ERROR: 模型制品操作失败。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
