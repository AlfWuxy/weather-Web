#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Git HEAD 候选 blob 确定性冻结微信小程序发布材料。"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    from scripts import update_env_value as env_updater
    from scripts import wechat_release_contract as contract
except ModuleNotFoundError:  # 允许直接执行 scripts 下的文件。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import update_env_value as env_updater
    from scripts import wechat_release_contract as contract

DEFAULT_FORM_NAME = ".env.wechat-release"
LOCK_NAME = "wechat-release-finalize.lock"
MAX_FORM_BYTES = 64 * 1024
MAX_CONTENT_BYTES = 2 * 1024 * 1024
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
FORM_KEYS = frozenset({
    "WECHAT_MINIPROGRAM_NAME", "WECHAT_EFFECTIVE_DATE", "WX_MINIPROGRAM_PRIVACY_VERSION",
    "WECHAT_RELEASE_VERSION", "WECHAT_FORM_READY", "WECHAT_CATEGORY_CONFIRMED",
})

# 兼容仓库内既有调用方导入这些常量。
CONTENT_PATHS = contract.CONTENT_PATHS
RELEASE_ARTIFACTS = contract.RELEASE_ARTIFACTS
FREEZE_KEYS = contract.FREEZE_KEYS

class ReleaseFinalizeError(RuntimeError):
    """发布流程检测到未知或并发状态并失败关闭。"""

@dataclass(frozen=True)
class FormSnapshot:
    path: Path
    content: bytes
    fingerprint: tuple[int, ...]
    fields: contract.PublicReleaseFields

def _git(root: Path, *arguments: str, allow_failure: bool = False) -> bytes | None:
    try:
        result = subprocess.run(("git", "-C", str(root), *arguments), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=False)
    except OSError as error:
        if allow_failure:
            return None
        raise ReleaseFinalizeError("Git 状态无法验证。") from error
    if result.returncode != 0:
        if allow_failure:
            return None
        raise ReleaseFinalizeError("Git 状态无法验证。")
    return result.stdout

def _resolve_root(repo_root: Path | str | None) -> Path:
    candidate = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    try:
        root = candidate.resolve(strict=True)
        top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve(strict=True)
    except (AttributeError, UnicodeDecodeError, OSError, RuntimeError) as error:
        raise ReleaseFinalizeError("仓库根目录无法验证。") from error
    if root != top or not root.is_dir():
        raise ReleaseFinalizeError("仓库根目录必须是 Git 工作树顶层。")
    return root

def _head(root: Path) -> str:
    try:
        value = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except (AttributeError, UnicodeDecodeError) as error:
        raise ReleaseFinalizeError("Git HEAD 无法验证。") from error
    if not COMMIT_RE.fullmatch(value):
        raise ReleaseFinalizeError("Git HEAD 无法验证。")
    return value

def _status(root: Path) -> bytes:
    return _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                "--ignore-submodules=none") or b""

def _status_paths(status: bytes) -> set[str]:
    records = status.split(b"\0")
    paths: set[str] = set()
    index = 0
    try:
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                raise UnicodeDecodeError("utf-8", record, 0, len(record), "short")
            paths.add(record[3:].decode("utf-8"))
            if record[:1] in {b"R", b"C"} or record[1:2] in {b"R", b"C"}:
                if index < len(records) and records[index]:
                    paths.add(records[index].decode("utf-8"))
                    index += 1
    except UnicodeDecodeError as error:
        raise ReleaseFinalizeError("Git 状态路径无法验证。") from error
    return paths

def _git_path(root: Path, name: str) -> Path:
    try:
        path = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-path", name)
                    .decode("utf-8").strip())
    except (AttributeError, UnicodeDecodeError) as error:
        raise ReleaseFinalizeError("Git 私有路径无法验证。") from error
    if not path.is_absolute() or path.name != name:
        raise ReleaseFinalizeError("Git 私有路径无法验证。")
    return path

@contextmanager
def _release_lock(root: Path):
    """合作式互斥锁；威胁模型不防同一维护者主动绕锁。"""
    path = _git_path(root, LOCK_NAME)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            raise ReleaseFinalizeError("另一个发布命令正在执行。") from error
        raise ReleaseFinalizeError("发布互斥锁无法获取。") from error
    try:
        _cleanup_content_temps(root)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)

def _read_regular(path: Path, *, maximum: int, mode: int) -> tuple[bytes, tuple[int, ...]]:
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode) or stat.S_ISLNK(initial.st_mode):
            raise OSError
        if stat.S_IMODE(initial.st_mode) != mode or initial.st_size > maximum:
            raise OSError
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            content = source.read(maximum + 1)
            finished = os.fstat(source.fileno())
        final = path.lstat()
    except OSError as error:
        raise ReleaseFinalizeError("所需文件类型、权限或大小异常。") from error
    if len(content) > maximum or not (_fingerprint(initial) == _fingerprint(opened)
                                      == _fingerprint(finished) == _fingerprint(final)):
        raise ReleaseFinalizeError("所需文件在读取期间发生变化。")
    return content, _fingerprint(final)

def _cleanup_temps(path: Path, prefix: str, maximum: int, modes: set[int]) -> None:
    """仅清理合作式锁保护下、由本工具命名的安全普通临时文件。"""
    try:
        for entry in path.parent.iterdir():
            if not entry.name.startswith(prefix):
                continue
            value = entry.lstat()
            if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                    or stat.S_IMODE(value.st_mode) not in modes or value.st_size > maximum):
                raise OSError
            entry.unlink()
    except OSError as error:
        raise ReleaseFinalizeError("检测到无法安全收敛的发布暂存文件。") from error

def _cleanup_content_temps(root: Path) -> None:
    for relative in CONTENT_PATHS:
        path = root / relative
        _cleanup_temps(path, f".{path.name}.finalize.", MAX_CONTENT_BYTES, {0o600, 0o644})

def _decode_value(raw: str) -> str:
    value = raw.strip()
    if value[:1] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ReleaseFinalizeError("私密发布表单格式异常。")
        return value[1:-1]
    if value[-1:] in {"'", '"'}:
        raise ReleaseFinalizeError("私密发布表单格式异常。")
    return value

def _parse_values(content: bytes, keys) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseFinalizeError("私密发布表单必须是 UTF-8 文本。") from error
    selected = set(keys)
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in selected:
            continue
        if key in values:
            raise ReleaseFinalizeError("私密发布表单字段必须唯一。")
        values[key] = _decode_value(raw_value)
    if set(values) != selected:
        raise ReleaseFinalizeError("私密发布表单缺少必填字段。")
    return values

def _nonfreeze_lines(content: bytes) -> tuple[bytes, ...]:
    keys = {key.encode("ascii") for key in FREEZE_KEYS}
    return tuple(line for line in content.splitlines(keepends=True)
                 if line.split(b"=", 1)[0].strip() not in keys)

def _form_path(root: Path, requested: Path | str | None) -> Path:
    relative = Path(requested) if requested is not None else Path(DEFAULT_FORM_NAME)
    path = Path(os.path.abspath(relative if relative.is_absolute() else root / relative))
    if path.parent != root:
        raise ReleaseFinalizeError("私密发布表单必须位于仓库根目录。")
    if _git(root, "check-ignore", "--quiet", "--", path.name, allow_failure=True) is None:
        raise ReleaseFinalizeError("私密发布表单必须被 Git 忽略。")
    temp_probe = f"{path.name}.tmp.probe"
    if _git(root, "check-ignore", "--quiet", "--", temp_probe, allow_failure=True) is None:
        raise ReleaseFinalizeError("私密表单原子暂存文件必须被 Git 忽略。")
    _cleanup_temps(path, f"{path.name}.tmp.", MAX_FORM_BYTES, {0o600})
    return path

def _read_form(root: Path, requested: Path | str | None) -> FormSnapshot:
    path = _form_path(root, requested)
    content, fingerprint = _read_regular(path, maximum=MAX_FORM_BYTES, mode=0o600)
    values = _parse_values(content, FORM_KEYS)
    fields = contract.PublicReleaseFields(values["WECHAT_MINIPROGRAM_NAME"],
        values["WECHAT_EFFECTIVE_DATE"], values["WX_MINIPROGRAM_PRIVACY_VERSION"],
        values["WECHAT_RELEASE_VERSION"])
    try:
        contract.validate_public_fields(
            fields,
            form_ready=values["WECHAT_FORM_READY"],
            category_confirmed=values["WECHAT_CATEGORY_CONFIRMED"],
        )
    except contract.ReleaseContractError as error:
        raise ReleaseFinalizeError("私密发布表单公开字段不符合发布合同。") from error
    return FormSnapshot(path, content, fingerprint, fields)

def _assert_form(root: Path, snapshot: FormSnapshot) -> None:
    if _form_path(root, snapshot.path) != snapshot.path:
        raise ReleaseFinalizeError("私密发布表单路径发生变化。")
    content, fingerprint = _read_regular(snapshot.path, maximum=MAX_FORM_BYTES, mode=0o600)
    if content != snapshot.content or fingerprint != snapshot.fingerprint:
        raise ReleaseFinalizeError("私密发布表单在命令执行期间发生变化。")

def _head_blobs(root: Path, head: str) -> dict[str, bytes]:
    result = {}
    for path in CONTENT_PATHS:
        content = _git(root, "cat-file", "blob", f"{head}:{path}", allow_failure=True)
        if content is None:
            raise ReleaseFinalizeError("Git HEAD 缺少发布材料。")
        result[path] = content
    return result

def _worktree_blobs(root: Path) -> dict[str, bytes]:
    result = {}
    for relative in CONTENT_PATHS:
        path = root / relative
        try:
            if path.resolve(strict=True) != path:
                raise OSError
        except (OSError, RuntimeError) as error:
            raise ReleaseFinalizeError("发布材料路径或类型异常。") from error
        result[relative], _ = _read_regular(path, maximum=MAX_CONTENT_BYTES, mode=0o644)
    return result

def _classify_head(blobs: dict[str, bytes], fields: contract.PublicReleaseFields):
    phases = []
    for path in CONTENT_PATHS[:-1]:
        if contract.has_final_marker(blobs[path]):
            phases.append("final")
            continue
        try:
            contract.render_artifact(path, blobs[path], fields)
            phases.append("candidate")
        except contract.ReleaseContractError:
            phases.append("unknown")
    try:
        if all(phase == "candidate" for phase in phases):
            return "candidate", contract.render_final(blobs, fields)
        if all(phase == "final" for phase in phases):
            contract.verify_final(blobs, fields)
            return "final", dict(blobs)
    except contract.ReleaseContractError:
        return "unknown", None
    return ("partial" if "unknown" not in phases else "unknown"), None

def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.finalize.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            os.fchmod(target.fileno(), 0o644)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()

def _require_finalize_context(root: Path, head: str, form: FormSnapshot,
                              candidate: dict[str, bytes], expected: dict[str, bytes]) -> dict[str, bytes]:
    before = _head(root)
    status = _status(root)
    if not _status_paths(status).issubset(set(CONTENT_PATHS)):
        raise ReleaseFinalizeError("工作树含发布材料以外的变化。")
    if _git(root, "diff", "--cached", "--quiet", allow_failure=True) is None:
        raise ReleaseFinalizeError("暂存区含未冻结状态。")
    current = _worktree_blobs(root)
    if any(current[path] not in {candidate[path], expected[path]} for path in CONTENT_PATHS):
        raise ReleaseFinalizeError("发布材料包含未知字节。")
    _assert_form(root, form)
    after = _head(root)
    if before != head or after != head:
        raise ReleaseFinalizeError("Git HEAD 在命令执行期间发生变化。")
    return current

def finalize_content(*, repo_root=None, wechat_form=None) -> bool:
    root = _resolve_root(repo_root)
    with _release_lock(root):
        form = _read_form(root, wechat_form)
        head = _head(root)
        head_blobs = _head_blobs(root, head)
        phase, expected = _classify_head(head_blobs, form.fields)
        current = _worktree_blobs(root)
        status = _status(root)
        if phase == "final":
            if status or current != head_blobs:
                raise ReleaseFinalizeError("正式 HEAD 要求工作树完全干净。")
            _assert_form(root, form)
            if _head(root) != head:
                raise ReleaseFinalizeError("Git HEAD 在命令执行期间发生变化。")
            return False
        if phase != "candidate" or expected is None:
            raise ReleaseFinalizeError("Git HEAD 发布材料状态未知或为局部冻结。")
        if not _status_paths(status).issubset(set(CONTENT_PATHS)):
            raise ReleaseFinalizeError("工作树含发布材料以外的变化。")
        if _git(root, "diff", "--cached", "--quiet", allow_failure=True) is None:
            raise ReleaseFinalizeError("暂存区含未冻结状态。")
        if any(current[path] not in {head_blobs[path], expected[path]} for path in CONTENT_PATHS):
            raise ReleaseFinalizeError("发布材料包含未知字节。")
        changed = any(current[path] != expected[path] for path in CONTENT_PATHS)
        for path in CONTENT_PATHS:
            if current[path] == expected[path]:
                continue
            _require_finalize_context(root, head, form, head_blobs, expected)
            _atomic_write(root / path, expected[path])
        final = _worktree_blobs(root)
        if final != expected:
            raise ReleaseFinalizeError("正式发布材料写后不一致。")
        try:
            contract.verify_final(final, form.fields)
        except contract.ReleaseContractError as error:
            raise ReleaseFinalizeError("正式发布材料不符合发布合同。") from error
        _require_finalize_context(root, head, form, head_blobs, expected)
        return changed

def record_freeze(*, repo_root=None, wechat_form=None) -> bool:
    root = _resolve_root(repo_root)
    with _release_lock(root):
        form = _read_form(root, wechat_form)
        if _status(root):
            raise ReleaseFinalizeError("record-freeze 要求工作树完全干净。")
        head = _head(root)
        blobs = _head_blobs(root, head)
        try:
            contract.verify_final(blobs, form.fields)
        except contract.ReleaseContractError as error:
            raise ReleaseFinalizeError("Git HEAD 尚未形成正式发布材料。") from error
        if _worktree_blobs(root) != blobs:
            raise ReleaseFinalizeError("工作树与 Git HEAD 不一致。")
        updates = {"WECHAT_RELEASE_VERSION": contract.EXPECTED_RELEASE_VERSION,
                   "WECHAT_TARGET_COMMIT_SHA": head,
                   **{key: hashlib.sha256(blobs[path]).hexdigest()
                      for key, path in RELEASE_ARTIFACTS}}
        _assert_form(root, form)
        if _head(root) != head or _status(root):
            raise ReleaseFinalizeError("Git 状态在记录冻结信息前发生变化。")
        try:
            changed = env_updater.update_env_values(
                form.path,
                updates,
                require_existing=True,
                expected_content=form.content.decode("utf-8"),
            )
        except (OSError, ValueError) as error:
            raise ReleaseFinalizeError("私密发布表单冻结失败。") from error
        content, _ = _read_regular(form.path, maximum=MAX_FORM_BYTES, mode=0o600)
        if (_parse_values(content, FREEZE_KEYS) != updates
                or _nonfreeze_lines(content) != _nonfreeze_lines(form.content)):
            raise ReleaseFinalizeError("私密发布表单冻结写后不一致。")
        _read_form(root, form.path)
        before = _head(root)
        status = _status(root)
        after = _head(root)
        if before != head or after != head or status:
            raise ReleaseFinalizeError("Git 状态在表单替换后发生变化。")
        return changed

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="确定性冻结宜老天气通微信小程序发布内容。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("finalize-content", "record-freeze"):
        child = subparsers.add_parser(command)
        child.add_argument("--repo-root", type=Path, default=None)
        child.add_argument("--wechat-form", type=Path, default=None)
    return parser

def main(argv=None) -> int:
    options = _parser().parse_args(argv)
    try:
        operation = finalize_content if options.command == "finalize-content" else record_freeze
        operation(repo_root=options.repo_root, wechat_form=options.wechat_form)
    except ReleaseFinalizeError as error:
        print(f"release-finalizer: blocked: {error}", file=sys.stderr)
        return 2
    print(f"release-finalizer: {options.command}: ok")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
