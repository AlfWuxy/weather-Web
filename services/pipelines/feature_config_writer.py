# -*- coding: utf-8 -*-
"""训练脚本共享的特征配置与运行制品清单写入工具。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

import sklearn


ARTIFACT_NAMES = (
    "disease_predictor.pkl",
    "scaler.pkl",
    "label_encoder.pkl",
)
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024


class _ArtifactReadError(RuntimeError):
    """运行制品无法生成可信摘要。"""


def _required_open_flags() -> int:
    """返回可阻止符号链接、阻塞与描述符继承的只读标志。"""
    required = ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required):
        raise _ArtifactReadError("safe_open_unavailable")
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    """生成用于检测文件替换与写入竞态的元数据指纹。"""
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


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    """返回权限收紧前后必须保持不变的文件身份。"""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _validate_regular_file(
    metadata: os.stat_result,
) -> None:
    """制品必须是非空、单硬链接且大小受限的普通文件。"""
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not (0 < metadata.st_size <= MAX_ARTIFACT_BYTES)
    ):
        raise _ArtifactReadError("artifact_metadata_invalid")


def _read_artifact_spec(path: Path) -> dict[str, str | int]:
    """稳定读取一个普通文件，并生成发布清单所需摘要。"""
    descriptor: int | None = None
    try:
        lexical = path.lstat()
        _validate_regular_file(lexical)
        descriptor = os.open(path, _required_open_flags())
        opened = os.fstat(descriptor)
        _validate_regular_file(opened)
        if _fingerprint(lexical) != _fingerprint(opened):
            raise _ArtifactReadError("artifact_changed")
        if opened.st_uid != os.geteuid():
            raise _ArtifactReadError("artifact_owner_invalid")

        # joblib.dump 遵循进程 umask，常见默认值会生成 0644。
        # 只对已用 O_NOFOLLOW 打开的单链接本人文件收紧权限，再生成摘要。
        before_identity = _identity(opened)
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        _validate_regular_file(opened)
        after_chmod = path.lstat()
        if (
            _identity(opened) != before_identity
            or _fingerprint(opened) != _fingerprint(after_chmod)
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise _ArtifactReadError("artifact_changed")

        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                raise _ArtifactReadError("artifact_too_large")
            digest.update(chunk)

        after = os.fstat(descriptor)
        if (
            total != opened.st_size
            or _fingerprint(opened) != _fingerprint(after)
        ):
            raise _ArtifactReadError("artifact_changed")
    except _ArtifactReadError:
        raise
    except OSError as error:
        raise _ArtifactReadError("artifact_unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        final = path.lstat()
    except OSError as error:
        raise _ArtifactReadError("artifact_changed") from error
    if _fingerprint(opened) != _fingerprint(final):
        raise _ArtifactReadError("artifact_changed")

    return {
        "sha256": digest.hexdigest(),
        "size_bytes": total,
    }


def _stale_runtime_artifacts(reason: str) -> dict[str, Any]:
    """生成必定无法通过正式发布 exact-schema 校验的显式状态。"""
    return {
        "status": "stale",
        "reason": reason,
        "expected_sklearn_version": sklearn.__version__,
        "files": {},
    }


def _build_runtime_artifacts(models_directory: Path) -> dict[str, Any]:
    """从本次训练写出的固定三文件重新生成完整清单。"""
    try:
        files = {
            filename: _read_artifact_spec(models_directory / filename)
            for filename in ARTIFACT_NAMES
        }
    except _ArtifactReadError as error:
        return _stale_runtime_artifacts(str(error))
    except Exception:
        # 未知异常也要覆盖旧摘要，确保发布链保持 fail-closed。
        return _stale_runtime_artifacts("artifact_digest_error")

    return {
        "expected_sklearn_version": sklearn.__version__,
        "files": files,
    }


def write_feature_config(
    config_path: str | Path,
    training_config: dict[str, Any],
) -> None:
    """原子写入训练配置，并基于当前三份制品重建发布清单。"""
    if not isinstance(training_config, dict):
        raise TypeError("training_config 必须是 dict")

    target = Path(config_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    output_config = dict(training_config)
    output_config["runtime_artifacts"] = _build_runtime_artifacts(
        target.parent,
    )

    try:
        target_mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        target_mode = 0o644

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(
                output_config,
                temporary_file,
                ensure_ascii=False,
                indent=2,
            )
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        temporary_path.chmod(target_mode)
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
