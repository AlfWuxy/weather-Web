#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理超过保留期的部署事务副本，并保留去敏故障提示。"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path


ATTENTION_MARKERS = ("ROLLBACK_REQUIRED.txt", "POST_COMMIT_ATTENTION.txt")
TERMINAL_MARKERS = ("COMMITTED", "ROLLED_BACK")
ACTIVITY_MARKERS = (
    "ACTIVATION_STARTED",
    "FORWARD_ONLY_REQUIRED",
    "PUBLIC_START_ATTEMPTED",
    "qweather-key-transition.json",
    "formal-smoke-lease.journal",
)
RECOVERY_CONFIRMED_MARKER = "RECOVERY_CONFIRMED"
CONTROL_MARKERS = (
    *ACTIVITY_MARKERS,
    *ATTENTION_MARKERS,
    *TERMINAL_MARKERS,
    RECOVERY_CONFIRMED_MARKER,
)
MODE_600_MARKERS = {
    RECOVERY_CONFIRMED_MARKER,
    "FORWARD_ONLY_REQUIRED",
    "PUBLIC_START_ATTEMPTED",
    "qweather-key-transition.json",
    "formal-smoke-lease.journal",
    *TERMINAL_MARKERS,
}
TERMINAL_PAYLOADS = {
    "COMMITTED": {b"success\n"},
    "ROLLED_BACK": {b"success\n", b"pre-mutation\n"},
}
MAX_CONTROL_MARKER_BYTES = 64 * 1024


def _path_exists_without_following(path: Path) -> bool:
    """识别普通文件、链接和其他异常节点，不跟随符号链接。"""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _validate_directory(
    path: Path,
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> os.stat_result:
    """验证清理控制目录，禁止链接、可写共享目录和属主漂移。"""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"控制目录不可用: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (owner_uid is not None and metadata.st_uid != owner_uid)
        or (owner_gid is not None and metadata.st_gid != owner_gid)
    ):
        raise ValueError(f"控制目录所有权、权限或类型不安全: {path}")
    return metadata


def _same_trusted_directory(path: Path, expected: os.stat_result) -> bool:
    """在破坏性操作前重新确认控制目录身份没有被替换。"""
    try:
        current = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and not stat.S_ISLNK(current.st_mode)
        and _directory_identity(current) == _directory_identity(expected)
        and current.st_uid == expected.st_uid
        and current.st_gid == expected.st_gid
        and not stat.S_IMODE(current.st_mode) & 0o022
    )


def _trusted_marker_metadata(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    required_mode: int | None = None,
) -> os.stat_result | None:
    """部署控制标记必须是由控制目录属主持有的普通文件。"""
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_gid != owner_gid
        or (
            required_mode is not None
            and stat.S_IMODE(metadata.st_mode) != required_mode
        )
    ):
        return None
    return metadata


def _read_trusted_marker_bytes(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    required_mode: int,
) -> bytes | None:
    """通过无跟随文件描述符读取小型控制标记，并复核 inode。"""
    expected = _trusted_marker_metadata(
        path,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        required_mode=required_mode,
    )
    if expected is None:
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or _directory_identity(current) != _directory_identity(expected)
            or current.st_uid != owner_uid
            or current.st_gid != owner_gid
            or stat.S_IMODE(current.st_mode) != required_mode
        ):
            return None
        chunks = []
        remaining = MAX_CONTROL_MARKER_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        return payload if len(payload) <= MAX_CONTROL_MARKER_BYTES else None
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _valid_recovery_confirmation(
    marker: Path,
    *,
    release_directory: Path | None,
    owner_uid: int,
    owner_gid: int,
) -> bool:
    """只接受与激活事务相同的完整人工恢复确认契约。"""
    if release_directory is None:
        return False
    payload = _read_trusted_marker_bytes(
        marker,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        required_mode=0o600,
    )
    if payload is None:
        return False
    try:
        values = {}
        for line in payload.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or not key or not value or key in values:
                return False
            values[key] = value
        if set(values) != {"confirmed_at", "confirmed_before_release"}:
            return False
        datetime.strptime(values["confirmed_at"], "%Y-%m-%dT%H:%M:%SZ")
        confirmed_release = Path(values["confirmed_before_release"])
        return (
            confirmed_release.is_absolute()
            and str(confirmed_release) == str(confirmed_release.resolve(strict=False))
            and confirmed_release.parent.resolve(strict=True) == release_directory
            and not any(
                character in values["confirmed_before_release"]
                for character in ("\x00", "\t", "\r", "\n")
            )
        )
    except (OSError, UnicodeError, ValueError):
        return False


def _valid_terminal_marker(
    name: str,
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> bool:
    payload = _read_trusted_marker_bytes(
        path,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        required_mode=0o600,
    )
    return payload in TERMINAL_PAYLOADS[name] if payload is not None else False


def _read_file_at(
    directory_fd: int,
    name: str,
    *,
    maximum: int,
    owner_uid: int,
    owner_gid: int,
    required_mode: int,
) -> bytes | None:
    """从已固定目录读取普通文件，拒绝链接、替换和超大内容。"""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        expected = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(expected.st_mode)
            or expected.st_uid != owner_uid
            or expected.st_gid != owner_gid
            or stat.S_IMODE(expected.st_mode) != required_mode
            or expected.st_nlink != 1
        ):
            return None
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        return None
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or _directory_identity(current) != _directory_identity(expected)
            or current.st_uid != owner_uid
            or current.st_gid != owner_gid
            or stat.S_IMODE(current.st_mode) != required_mode
            or current.st_nlink != 1
        ):
            return None
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        return payload if len(payload) <= maximum else None
    finally:
        os.close(descriptor)


def _valid_existing_alert(
    alert_fd: int,
    name: str,
    payload: bytes,
    *,
    owner_uid: int,
    owner_gid: int,
) -> bool:
    try:
        metadata = os.stat(name, dir_fd=alert_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == owner_uid
        and metadata.st_gid == owner_gid
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_nlink == 1
        and _read_file_at(
            alert_fd,
            name,
            maximum=len(payload),
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            required_mode=0o600,
        )
        == payload
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("无法完整写入告警文件")
        remaining = remaining[written:]


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(path, flags)


def _chmod_trusted_directory(
    path: Path,
    metadata: os.stat_result,
    mode: int,
) -> os.stat_result:
    """通过无跟随目录描述符收紧权限，并确认路径仍指向同一 inode。"""
    descriptor = _open_directory(path)
    try:
        opened = os.fstat(descriptor)
        if _directory_identity(opened) != _directory_identity(metadata):
            raise ValueError(f"控制目录在权限收紧前被替换: {path}")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        updated = os.fstat(descriptor)
        if (
            _directory_identity(updated) != _directory_identity(metadata)
            or stat.S_IMODE(updated.st_mode) != mode
        ):
            raise ValueError(f"控制目录权限收紧失败: {path}")
    finally:
        os.close(descriptor)
    current = path.lstat()
    if _directory_identity(current) != _directory_identity(metadata):
        raise ValueError(f"控制目录在权限收紧后被替换: {path}")
    return current


def _publish_alert(
    alert_root: Path,
    alert_fd: int,
    name: str,
    payload: bytes,
    *,
    owner_uid: int,
    owner_gid: int,
) -> bool:
    """以无覆盖硬链接原子发布告警，已有内容必须完全一致。"""
    try:
        os.stat(name, dir_fd=alert_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        return _valid_existing_alert(
            alert_fd,
            name,
            payload,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )

    temporary_name = f".{name}.next.{os.getpid()}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    temporary_created = False
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=alert_fd)
        temporary_created = True
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        published = False
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=alert_fd,
                dst_dir_fd=alert_fd,
                follow_symlinks=False,
            )
            os.fsync(alert_fd)
            published = True
        except FileExistsError:
            published = _valid_existing_alert(
                alert_fd,
                name,
                payload,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=alert_fd)
                os.fsync(alert_fd)
            except FileNotFoundError:
                pass
    if published:
        # 再次验证目录路径，防止调用者把已打开目录误认成另一路径。
        try:
            current = alert_root.lstat()
        except OSError:
            return False
        if _directory_identity(current) != _directory_identity(os.fstat(alert_fd)):
            return False
    return published


def _prepare_control_directories(
    state_dir: Path,
) -> tuple[Path, Path, Path, os.stat_result, os.stat_result, os.stat_result | None]:
    """逐层创建并固定控制目录，禁止任何既有链接进入删除路径。"""
    raw_state_dir = Path(state_dir).expanduser()
    if not raw_state_dir.is_absolute() or raw_state_dir == Path("/"):
        raise ValueError("state_dir 必须是安全的专用绝对目录")
    if str(raw_state_dir) != str(raw_state_dir.resolve(strict=False)):
        raise ValueError("state_dir 必须是无链接、无路径折叠的规范绝对目录")
    state_dir = raw_state_dir
    if not _path_exists_without_following(state_dir):
        try:
            state_dir.mkdir(mode=0o750)
        except OSError as exc:
            raise ValueError(f"无法安全创建 state_dir: {state_dir}") from exc
    state_metadata = _validate_directory(state_dir, owner_uid=os.geteuid())

    backups = state_dir / "backups"
    if not _path_exists_without_following(backups):
        try:
            backups.mkdir(mode=0o700)
        except OSError as exc:
            raise ValueError(f"无法安全创建 backups: {backups}") from exc
    backups_metadata = _validate_directory(backups, owner_uid=state_metadata.st_uid)

    transaction_root = backups / "deploy-transactions"
    if not _path_exists_without_following(transaction_root):
        try:
            transaction_root.mkdir(mode=0o700)
        except OSError as exc:
            raise ValueError(f"无法安全创建事务目录: {transaction_root}") from exc
    transaction_metadata = _validate_directory(
        transaction_root,
        owner_uid=backups_metadata.st_uid,
        owner_gid=backups_metadata.st_gid,
    )
    transaction_metadata = _chmod_trusted_directory(
        transaction_root,
        transaction_metadata,
        0o700,
    )
    transaction_metadata = _validate_directory(
        transaction_root,
        owner_uid=backups_metadata.st_uid,
        owner_gid=backups_metadata.st_gid,
    )

    alert_root = backups / "deploy-retention-alerts"
    alert_metadata = None
    if _path_exists_without_following(alert_root):
        alert_metadata = _validate_directory(
            alert_root,
            owner_uid=backups_metadata.st_uid,
            owner_gid=backups_metadata.st_gid,
        )
        alert_metadata = _chmod_trusted_directory(
            alert_root,
            alert_metadata,
            0o700,
        )
        alert_metadata = _validate_directory(
            alert_root,
            owner_uid=backups_metadata.st_uid,
            owner_gid=backups_metadata.st_gid,
        )
    return (
        backups,
        transaction_root,
        alert_root,
        backups_metadata,
        transaction_metadata,
        alert_metadata,
    )
def prune_deploy_transactions(
    state_dir: Path,
    *,
    release_root: Path | None = None,
    now: datetime | None = None,
    retention_days: int = 30,
    preserve_names=(),
):
    """只处理 state/backups/deploy-transactions 的直接可信子目录。"""
    retention_days = int(retention_days)
    if not 1 <= retention_days <= 365:
        raise ValueError("retention_days 必须在 1 至 365 之间")
    (
        backups,
        transaction_root,
        alert_root,
        backups_metadata,
        transaction_root_metadata,
        alert_root_metadata,
    ) = _prepare_control_directories(Path(state_dir))
    state_dir = transaction_root.parent.parent
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("当前 Python 不支持安全的目录树清理")

    owner_uid = transaction_root_metadata.st_uid
    owner_gid = transaction_root_metadata.st_gid
    try:
        configured_release_root = (
            Path(release_root).expanduser()
            if release_root is not None
            else Path(f"{state_dir}-deploy")
        )
        release_directory = (
            configured_release_root.resolve(strict=True) / "releases"
        ).resolve(strict=True)
        if not release_directory.is_dir():
            release_directory = None
    except OSError:
        # 无法确认 release 根时保持故障事务，禁止把无效确认当作删除授权。
        release_directory = None
    preserve = {str(name) for name in preserve_names if str(name)}
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cutoff_timestamp = (reference - timedelta(days=retention_days)).timestamp()
    removed = []
    alerts = []
    skipped_symlinks = []
    preserved_unresolved = []

    backups_fd = _open_directory(backups)
    transaction_root_fd = _open_directory(transaction_root)
    try:
        with os.scandir(transaction_root_fd) as entries:
            entry_names = sorted(entry.name for entry in entries)
        for entry_name in entry_names:
            if entry_name in preserve:
                continue
            try:
                entry_metadata = os.stat(
                    entry_name,
                    dir_fd=transaction_root_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                preserved_unresolved.append(entry_name)
                continue
            if stat.S_ISLNK(entry_metadata.st_mode):
                skipped_symlinks.append(entry_name)
                continue
            if not stat.S_ISDIR(entry_metadata.st_mode):
                continue
            if entry_metadata.st_mtime >= cutoff_timestamp:
                continue
            if (
                "\n" in entry_name
                or "\r" in entry_name
                or entry_metadata.st_uid != owner_uid
                or entry_metadata.st_gid != owner_gid
                or stat.S_IMODE(entry_metadata.st_mode) & 0o022
            ):
                preserved_unresolved.append(entry_name)
                continue

            entry = transaction_root / entry_name
            marker_paths = {name: entry / name for name in CONTROL_MARKERS}
            marker_present = {
                name: _path_exists_without_following(path)
                for name, path in marker_paths.items()
            }
            invalid_control_state = any(
                marker_present[name]
                and _trusted_marker_metadata(
                    path,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                    required_mode=0o600 if name in MODE_600_MARKERS else None,
                )
                is None
                for name, path in marker_paths.items()
            )
            terminal_names = [name for name in TERMINAL_MARKERS if marker_present[name]]
            valid_terminal = (
                len(terminal_names) == 1
                and _valid_terminal_marker(
                    terminal_names[0],
                    marker_paths[terminal_names[0]],
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                )
            )
            confirmation_present = marker_present[RECOVERY_CONFIRMED_MARKER]
            confirmation_valid = _valid_recovery_confirmation(
                marker_paths[RECOVERY_CONFIRMED_MARKER],
                release_directory=release_directory,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            attention_names = [
                name for name in ATTENTION_MARKERS if marker_present[name]
            ]
            activity_present = any(marker_present[name] for name in ACTIVITY_MARKERS)
            unresolved = (
                invalid_control_state
                or len(terminal_names) > 1
                or (bool(terminal_names) and not valid_terminal)
                or (confirmation_present and not confirmation_valid)
                or (bool(attention_names) and not confirmation_valid)
                or (activity_present and not valid_terminal and not confirmation_valid)
            )
            if unresolved:
                preserved_unresolved.append(entry_name)
                continue

            alert_name = None
            if attention_names:
                if alert_root_metadata is None:
                    alert_root_created = False
                    try:
                        os.mkdir(
                            alert_root.name,
                            0o700,
                            dir_fd=backups_fd,
                        )
                        alert_root_created = True
                    except FileExistsError:
                        pass
                    alert_root_metadata = _validate_directory(
                        alert_root,
                        owner_uid=backups_metadata.st_uid,
                        owner_gid=backups_metadata.st_gid,
                    )
                    alert_root_metadata = _chmod_trusted_directory(
                        alert_root,
                        alert_root_metadata,
                        0o700,
                    )
                    alert_root_metadata = _validate_directory(
                        alert_root,
                        owner_uid=backups_metadata.st_uid,
                        owner_gid=backups_metadata.st_gid,
                    )
                    if alert_root_created:
                        # 新目录项必须先在父目录耐久化，随后才允许删除事务副本。
                        os.fsync(backups_fd)
                if not _same_trusted_directory(alert_root, alert_root_metadata):
                    preserved_unresolved.append(entry_name)
                    continue
                alert_payload = (
                    "部署事务敏感副本已按保留期清理。\n"
                    f"事务: {entry_name}\n"
                    f"原故障标记: {', '.join(attention_names)}\n"
                ).encode("utf-8")
                alert_name = f"{entry_name}.txt"
                alert_fd = _open_directory(alert_root)
                try:
                    if not _publish_alert(
                        alert_root,
                        alert_fd,
                        alert_name,
                        alert_payload,
                        owner_uid=owner_uid,
                        owner_gid=owner_gid,
                    ):
                        preserved_unresolved.append(entry_name)
                        continue
                finally:
                    os.close(alert_fd)

            try:
                current_entry = os.stat(
                    entry_name,
                    dir_fd=transaction_root_fd,
                    follow_symlinks=False,
                )
            except OSError:
                preserved_unresolved.append(entry_name)
                continue
            if (
                not _same_trusted_directory(
                    transaction_root,
                    transaction_root_metadata,
                )
                or _directory_identity(current_entry) != _directory_identity(entry_metadata)
                or not stat.S_ISDIR(current_entry.st_mode)
                or stat.S_ISLNK(current_entry.st_mode)
                or current_entry.st_uid != owner_uid
                or current_entry.st_gid != owner_gid
                or stat.S_IMODE(current_entry.st_mode) & 0o022
            ):
                preserved_unresolved.append(entry_name)
                continue

            # 使用固定的父目录描述符，确保目录路径被替换时不会越界删除。
            shutil.rmtree(entry_name, dir_fd=transaction_root_fd)
            os.fsync(transaction_root_fd)
            removed.append(entry_name)
            if alert_name is not None:
                alerts.append(alert_name)
    finally:
        os.close(transaction_root_fd)
        os.close(backups_fd)

    return {
        "retention_days": retention_days,
        "removed": removed,
        "attention_alerts": alerts,
        "skipped_symlinks": skipped_symlinks,
        "preserved_unresolved": preserved_unresolved,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prune private deploy transaction backups.")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--preserve-name", action="append", default=[])
    args = parser.parse_args(argv)
    result = prune_deploy_transactions(
        args.state_dir,
        release_root=args.release_root,
        retention_days=args.retention_days,
        preserve_names=args.preserve_name,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
