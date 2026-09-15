"""原件与冻结包使用外置密钥加密，磁盘只保存随机路径。"""
import hashlib
import hmac
import os
from pathlib import Path
from uuid import uuid4
from cryptography.fernet import Fernet
from flask import current_app


def _cipher():
    key = current_app.config.get('PILOT_STORAGE_KEY')
    if not key:
        raise ValueError('必须配置 PILOT_STORAGE_KEY，不能自动生成密钥')
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (TypeError, ValueError) as exc:
        raise ValueError('PILOT_STORAGE_KEY 格式无效') from exc


def _root():
    value = current_app.config.get('PILOT_STORAGE_DIR')
    if not value or not Path(value).is_absolute():
        raise ValueError('PILOT_STORAGE_DIR 必须是独立私有绝对路径')
    path = Path(value)
    project = Path(__file__).resolve().parents[2]
    if path.is_symlink() or path.resolve().is_relative_to(project) or path == Path('/'):
        raise ValueError('私有目录不得位于项目或公共文件目录')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path.resolve()


def store_bytes(data, kind='raw'):
    if kind not in {'raw', 'snapshot', 'model'} or not isinstance(data, bytes):
        raise ValueError('存储对象格式无效')
    encrypted = _cipher().encrypt(data)
    root = _root()
    name = f'{kind}-{uuid4().hex}.enc'
    descriptor = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(encrypted)
        handle.flush()
        os.fsync(handle.fileno())
    return name


def read_bytes(path):
    root = _root()
    if not isinstance(path, str) or Path(path).name != path or not path.endswith('.enc'):
        raise ValueError('存储路径无效')
    target = root / path
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError('存储路径无效')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(target, flags), 'rb') as handle:
        return _cipher().decrypt(handle.read())


def private_digest(institution_id, value):
    # 使用外置密钥派生 HMAC，防止低熵身份字段被离线枚举。
    _cipher()
    key = current_app.config['PILOT_STORAGE_KEY']
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(hashlib.sha256(key).digest(), f'{institution_id}:{value}'.encode(), hashlib.sha256).hexdigest()


def delete_bytes(path):
    """仅由受控保存期限流程调用；不能删除目录或任意系统路径。"""
    root = _root()
    if not isinstance(path, str) or Path(path).name != path or not path.endswith('.enc'):
        raise ValueError('存储路径无效')
    target = root / path
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError('存储路径无效')
    target.unlink(missing_ok=True)
