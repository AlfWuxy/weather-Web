"""采集材料的稳定去标识密钥；密钥不进入备份、接口或日志。"""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import secrets
import stat

from .community_store import CommunityError


def _unavailable():
    return CommunityError("记录导出的编号密钥暂不可用，请由维护人员检查；原记录已保留",
                          code="COLLECTION_KEY_UNAVAILABLE", status=503)


def account_collection_key(master_key, subject):
    """账户和用途分别绑定；轮换站点会话密钥后不得自动合并新旧代号。"""
    if isinstance(master_key, str):
        master_key = master_key.encode("utf-8")
    if not isinstance(master_key, bytes) or len(master_key) < 32 or not isinstance(subject, str) or not subject:
        raise _unavailable()
    message = json.dumps(["yilao-field-collection-account-key-v1", subject],
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hmac.new(master_key, message, sha256).digest()


def local_collection_key(database_path):
    """本机服务启动时准备私有密钥；恢复数据库时须保留对应密钥文件。"""
    # macOS 的 /var 是 /private/var 的系统别名；先规范数据库目录，密钥本身仍禁止链接。
    database = Path(database_path).expanduser().resolve()
    path = database.with_name(database.name + ".collection-key")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as file:
                file.write(secrets.token_bytes(32))
                file.flush()
                os.fsync(file.fileno())
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_size != 32 or info.st_nlink != 1):
                raise _unavailable()
            key = file.read(33)
        if len(key) != 32:
            raise _unavailable()
        return key
    except OSError:
        # 异常不返回路径或密钥；并发首次启动若读到未完成文件则安全失败。
        raise _unavailable() from None
