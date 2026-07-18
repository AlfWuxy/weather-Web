# -*- coding: utf-8 -*-
"""第三方推送与社区投影使用的安全跨进程文件锁。"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat

from flask import current_app, has_app_context


def _configured_dispatch_lock_path() -> Path:
    if not has_app_context():
        raise RuntimeError("push owner lock requires an app context")
    configured = str(current_app.config.get("DISPATCH_LOCK_PATH") or "").strip()
    if configured:
        path = Path(configured)
    elif current_app.testing or current_app.debug:
        # 测试与本地调试仍使用同一 instance 根，禁止退回每进程临时目录。
        path = Path(current_app.instance_path) / "case-weather-dispatch.lock"
    else:
        raise RuntimeError("DISPATCH_LOCK_PATH 未配置")
    if not path.is_absolute() or path == Path("/"):
        raise RuntimeError("DISPATCH_LOCK_PATH 必须是安全绝对路径")
    return path


def _open_scoped_lock_file(
    namespace: str,
    scope_key: str,
    *,
    filename: str | None = None,
):
    """在固定安全根目录下，为任意业务作用域创建不可穿越路径的锁文件。"""
    normalized_namespace = str(namespace or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{1,40}", normalized_namespace):
        raise ValueError("lock namespace 非法")
    normalized_scope_key = str(scope_key or "").strip()
    if not normalized_scope_key:
        raise ValueError("lock scope_key 不能为空")
    lock_digest = hashlib.sha256(normalized_scope_key.encode("utf-8")).hexdigest()
    normalized_filename = str(filename or f"{lock_digest}.lock").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", normalized_filename):
        raise ValueError("lock filename 非法")
    parent = _configured_dispatch_lock_path().parent
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(parent, directory_flags)
    lock_dir_fd = None
    try:
        directory_name = f"{normalized_namespace}-locks"
        try:
            os.mkdir(directory_name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        lock_dir_fd = os.open(directory_name, directory_flags, dir_fd=parent_fd)
        lock_dir_stat = os.fstat(lock_dir_fd)
        if not stat.S_ISDIR(lock_dir_stat.st_mode):
            raise RuntimeError("scoped lock directory is unsafe")
        os.fchmod(lock_dir_fd, 0o700)

        file_flags = os.O_RDWR | os.O_CREAT
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(
            normalized_filename,
            file_flags,
            0o600,
            dir_fd=lock_dir_fd,
        )
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(fd)
            raise RuntimeError("scoped lock is not a regular file")
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a+", encoding="utf-8")
    finally:
        if lock_dir_fd is not None:
            os.close(lock_dir_fd)
        os.close(parent_fd)


class ScopedFileLock:
    """可跨进程持有并显式释放的独立作用域锁。"""

    def __init__(self, namespace: str, scope_key: str, *, filename: str | None = None):
        self.namespace = namespace
        self.scope_key = scope_key
        self.filename = filename
        self.identity = (
            str(namespace),
            hashlib.sha256(str(scope_key).encode("utf-8")).hexdigest(),
        )
        self._handle = None

    def acquire(self):
        if self._handle is not None:
            return self
        handle = _open_scoped_lock_file(
            self.namespace,
            self.scope_key,
            filename=self.filename,
        )
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            handle.close()
            raise
        self._handle = handle
        return self

    def release(self):
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.release()


def community_projection_file_lock(community_code, status_date) -> ScopedFileLock:
    """返回按社区和日期隔离的投影锁，不让不同社区互相阻塞。"""
    normalized_code = str(community_code or "").strip()
    if not normalized_code:
        raise ValueError("community_code 不能为空")
    normalized_date = (
        status_date.isoformat()
        if hasattr(status_date, "isoformat")
        else str(status_date or "").strip()
    )
    if not normalized_date:
        raise ValueError("status_date 不能为空")
    return ScopedFileLock(
        "community-daily",
        f"{normalized_code}\x1f{normalized_date}",
    )


@contextmanager
def push_owner_lock(user_id: int):
    """串行化某个账号的发送、关闭、注销和成员级撤权。"""
    try:
        normalized_user_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("user_id 必须是正整数") from exc
    if normalized_user_id <= 0:
        raise ValueError("user_id 必须是正整数")
    # 保留旧版文件名，滚动切换期间新旧进程仍锁住同一个 owner 文件。
    with ScopedFileLock(
        "push-owner",
        f"user:{normalized_user_id}",
        filename=f"user-{normalized_user_id}.lock",
    ):
        yield
