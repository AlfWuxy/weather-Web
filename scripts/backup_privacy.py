#!/usr/bin/env python3
"""备份只能落入执行账号拥有的私有目录，不借chmod接受其他属主。"""
import os
from pathlib import Path
import stat
import sys


def protect_directory(root):
    root = Path(root)
    owner = os.geteuid()
    if root.is_symlink():
        raise ValueError("备份目录不得为链接")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.lstat().st_uid != owner:
        raise ValueError("备份目录属主不是备份执行账号，需单独审核归属后处理")
    os.chmod(root, 0o700)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if info.st_uid != owner:
                raise ValueError("备份历史文件属主与执行账号不一致，拒绝继续")
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise ValueError("备份目录存在链接或非普通文件，拒绝继续")
            os.chmod(path, 0o700 if stat.S_ISDIR(info.st_mode) else 0o600)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        protect_directory(sys.argv[1])
    except (OSError, ValueError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else "备份权限核验失败", file=sys.stderr)
        raise SystemExit(1)
