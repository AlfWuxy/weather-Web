import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


SYNTHETIC_KEY = b"synthetic-qweather-key-permission-regression\n"


CHILD_PROGRAM = r"""
import errno
import os
import sys


operation = sys.argv[1]
directory_fd = int(sys.argv[2])
filename = sys.argv[3]
expected_uid = int(sys.argv[4])
expected_gid = int(sys.argv[5])
expected_content = bytes.fromhex(sys.argv[6])

if os.geteuid() != expected_uid:
    print(f"unexpected euid: {os.geteuid()}", file=sys.stderr)
    raise SystemExit(20)
if os.getegid() != expected_gid:
    print(f"unexpected egid: {os.getegid()}", file=sys.stderr)
    raise SystemExit(21)
if os.getgroups():
    print(f"supplementary groups were retained: {os.getgroups()}", file=sys.stderr)
    raise SystemExit(22)

try:
    file_fd = os.open(
        filename,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
except OSError as error:
    if operation == "deny" and error.errno == errno.EACCES:
        raise SystemExit(0)
    print(
        f"unexpected open error: errno={error.errno} message={error}",
        file=sys.stderr,
    )
    raise SystemExit(30)

try:
    content = bytearray()
    while True:
        chunk = os.read(file_fd, 4096)
        if not chunk:
            break
        content.extend(chunk)
finally:
    os.close(file_fd)

if operation == "deny":
    print("root-only key unexpectedly readable", file=sys.stderr)
    raise SystemExit(31)
if operation != "read":
    print(f"unknown operation: {operation}", file=sys.stderr)
    raise SystemExit(32)
if bytes(content) != expected_content:
    print("synthetic key content mismatch", file=sys.stderr)
    raise SystemExit(33)
raise SystemExit(0)
"""


def _drop_to_user(uid: int, gid: int) -> None:
    # 清空附加组后再降权，避免 runner 的 root 组掩盖权限问题。
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def _run_as_nobody(
    *,
    uid: int,
    gid: int,
    directory_fd: int,
    operation: str,
    filename: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            CHILD_PROGRAM,
            operation,
            str(directory_fd),
            filename,
            str(uid),
            str(gid),
            SYNTHETIC_KEY.hex(),
        ],
        check=False,
        capture_output=True,
        pass_fds=(directory_fd,),
        preexec_fn=lambda: _drop_to_user(uid, gid),
        text=True,
    )
    assert completed.returncode == 0, (
        f"跨 UID 子进程失败，operation={operation}，filename={filename}，"
        f"returncode={completed.returncode}，stdout={completed.stdout!r}，"
        f"stderr={completed.stderr!r}"
    )


def _assert_owner_mode(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    links: int,
) -> os.stat_result:
    metadata = path.stat(follow_symlinks=False)
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_uid == uid
    assert metadata.st_gid == gid
    assert stat.S_IMODE(metadata.st_mode) == mode
    assert metadata.st_nlink == links
    return metadata


def test_qweather_key_stays_root_only_until_old_uid_is_quiesced(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("真实跨 UID 权限回归仅在 Linux 执行")
    if os.geteuid() != 0:
        pytest.skip("真实跨 UID 权限回归需要 root pytest")

    import pwd

    try:
        nobody = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("系统没有预置 nobody 用户，测试不会创建系统账号")

    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o755)
    os.chown(private_dir, 0, 0)
    os.chmod(private_dir, 0o755)

    pending_path = private_dir / ".qweather-jwt.pending-synthetic"
    final_path = private_dir / "qweather-jwt.pem"
    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    pending_fd = os.open(pending_path, create_flags, 0o600)
    try:
        os.fchown(pending_fd, 0, 0)
        os.fchmod(pending_fd, 0o600)
        os.write(pending_fd, SYNTHETIC_KEY)
        os.fsync(pending_fd)
    finally:
        os.close(pending_fd)

    os.link(pending_path, final_path, follow_symlinks=False)
    directory_fd = os.open(private_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
        pending_metadata = _assert_owner_mode(
            pending_path,
            uid=0,
            gid=0,
            mode=0o600,
            links=2,
        )
        final_metadata = _assert_owner_mode(
            final_path,
            uid=0,
            gid=0,
            mode=0o600,
            links=2,
        )
        assert pending_metadata.st_ino == final_metadata.st_ino

        _run_as_nobody(
            uid=nobody.pw_uid,
            gid=nobody.pw_gid,
            directory_fd=directory_fd,
            operation="deny",
            filename=pending_path.name,
        )
        _run_as_nobody(
            uid=nobody.pw_uid,
            gid=nobody.pw_gid,
            directory_fd=directory_fd,
            operation="deny",
            filename=final_path.name,
        )

        # 模拟旧服务退出后的顺序：先移除 pending 并落盘目录，再开放唯一 final。
        pending_path.unlink()
        os.fsync(directory_fd)
        assert not pending_path.exists()
        _assert_owner_mode(final_path, uid=0, gid=0, mode=0o600, links=1)

        os.chown(final_path, 0, nobody.pw_gid)
        os.chmod(final_path, 0o640)
        os.fsync(directory_fd)
        _assert_owner_mode(
            final_path,
            uid=0,
            gid=nobody.pw_gid,
            mode=0o640,
            links=1,
        )

        _run_as_nobody(
            uid=nobody.pw_uid,
            gid=nobody.pw_gid,
            directory_fd=directory_fd,
            operation="read",
            filename=final_path.name,
        )
    finally:
        os.close(directory_fd)
