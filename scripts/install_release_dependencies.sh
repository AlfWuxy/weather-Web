#!/usr/bin/env bash
# 在受限 transient unit 中安装依赖，或严格核验后低内存复用 current venv。

set -euo pipefail

if [ "$#" -lt 4 ] || [ "$#" -gt 7 ]; then
    echo "用法: $0 <release-app> <release-venv> <metadata-dir> <expected-lock-sha> [install|reuse-current] [current-link] [python-major-minor]" >&2
    exit 64
fi

RELEASE_APP="$1"
RELEASE_VENV="$2"
METADATA_DIR="$3"
EXPECTED_LOCK_SHA="$4"
INSTALL_MODE="${5:-install}"
CURRENT_LINK="${6:-}"
EXPECTED_PYTHON_MAJOR_MINOR="${7:-3.11}"
LOCK_FILE="$RELEASE_APP/requirements.lock"
FRESH_VENV_STARTED=0

for path in "$RELEASE_APP" "$RELEASE_VENV" "$METADATA_DIR"; do
    [[ "$path" = /* ]] || {
        echo "发布依赖安装只接受绝对路径。" >&2
        exit 64
    }
done
case "$INSTALL_MODE" in
    install) ;;
    reuse-current)
        [[ "$CURRENT_LINK" = /* ]] || {
            echo "复用依赖时 current 链接必须是绝对路径。" >&2
            exit 64
        }
        ;;
    *) echo "依赖安装模式只能是 install 或 reuse-current。" >&2; exit 64 ;;
esac
[[ "$EXPECTED_LOCK_SHA" =~ ^[0-9a-f]{64}$ ]] || {
    echo "requirements.lock 预期摘要格式不合法。" >&2
    exit 64
}
[[ "$EXPECTED_PYTHON_MAJOR_MINOR" =~ ^[0-9]+\.[0-9]+$ ]] || {
    echo "Python major.minor 格式不合法。" >&2
    exit 64
}
if [ ! -f "$LOCK_FILE" ] || [ -L "$LOCK_FILE" ]; then
    echo "requirements.lock 缺失或不是普通文件。" >&2
    exit 64
fi
if [ -e "$RELEASE_VENV" ] || [ -L "$RELEASE_VENV" ]; then
    echo "发布虚拟环境目标已存在，拒绝覆盖。" >&2
    exit 64
fi
if { [ -e "$METADATA_DIR" ] || [ -L "$METADATA_DIR" ]; } \
    && { [ ! -d "$METADATA_DIR" ] || [ -L "$METADATA_DIR" ]; }; then
    echo "发布私有元数据目录类型异常。" >&2
    exit 64
fi
for receipt_name in \
    python-version.txt \
    requirements-lock.sha256 \
    pip-inspect.json \
    dependency-receipt.json; do
    if [ -e "$METADATA_DIR/$receipt_name" ] \
        || [ -L "$METADATA_DIR/$receipt_name" ]; then
        echo "依赖收据目标已存在，拒绝覆盖。" >&2
        exit 64
    fi
done

umask 077
ACTUAL_LOCK_SHA="$(
    python3 -c \
        'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
        "$LOCK_FILE"
)"
if [ "$ACTUAL_LOCK_SHA" != "$EXPECTED_LOCK_SHA" ]; then
    echo "requirements.lock 摘要不匹配。" >&2
    exit 65
fi

cleanup_fresh_venv() {
    local status=$?
    trap - EXIT
    if [ "$status" -ne 0 ] && [ "$FRESH_VENV_STARTED" = 1 ]; then
        rm -rf --one-file-system -- "$RELEASE_VENV"
    fi
    exit "$status"
}
trap cleanup_fresh_venv EXIT

# 单个 helper 负责源证明、无跟随复制、候选复核和新收据，避免两套判断漂移。
run_dependency_helper() {
    python3 - "$@" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys


class ContractError(Exception):
    pass


def canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def lock_packages(path):
    packages = {}
    pattern = re.compile(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==([^\s\\;]+)"
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line[0].isspace():
            continue
        match = pattern.match(line)
        if match is None:
            raise ContractError("锁文件含无法核对的顶层条目")
        name = canonical_name(match.group(1))
        if name in packages:
            raise ContractError("锁文件含重复包名")
        packages[name] = match.group(2)
    if not packages:
        raise ContractError("锁文件没有包")
    return packages


def inspect_packages(payload):
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, list):
        raise ContractError("pip inspect 结构异常")
    packages = {}
    for item in installed:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        if not isinstance(metadata, dict):
            raise ContractError("pip inspect 缺少包元数据")
        name, version = metadata.get("name"), metadata.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ContractError("pip inspect 包名或版本异常")
        name = canonical_name(name)
        if name in packages:
            raise ContractError("pip inspect 含重复包")
        packages[name] = version
    return packages


def run_python(python, *arguments, before_exec=None):
    if before_exec is not None:
        before_exec()
    environment = os.environ.copy()
    # 核验过程不得向已冻结的虚拟环境写入 pyc，也不得继承外部模块注入路径。
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(python), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def live_contract(python, lock, expected_minor, before_exec=None):
    inspect_result = run_python(
        python,
        "-m",
        "pip",
        "inspect",
        "--local",
        before_exec=before_exec,
    )
    if inspect_result.returncode != 0:
        raise ContractError("pip inspect 失败")
    try:
        payload = json.loads(inspect_result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("pip inspect 输出异常") from exc
    if inspect_packages(payload) != lock_packages(lock):
        raise ContractError("已安装包集合或版本与锁不一致")
    if run_python(
        python, "-m", "pip", "check", before_exec=before_exec
    ).returncode != 0:
        raise ContractError("pip check 失败")
    version = run_python(
        python,
        "-c",
        (
            "import platform,sys;"
            "print(f'{sys.version_info.major}.{sys.version_info.minor}');"
            "print('Python ' + platform.python_version())"
        ),
        before_exec=before_exec,
    )
    lines = version.stdout.splitlines()
    if version.returncode != 0 or len(lines) != 2 or lines[0] != expected_minor:
        raise ContractError("Python major.minor 不一致")
    if run_python(
        python,
        "-c",
        "import gunicorn",
        before_exec=before_exec,
    ).returncode != 0:
        raise ContractError("缺少 gunicorn 模块")
    raw = inspect_result.stdout.encode("utf-8")
    return (raw if raw.endswith(b"\n") else raw + b"\n"), lines[1]


def require_safe_file(path, private=False, expected_gid=None):
    info = path.lstat()
    private_mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (
            private
            and (
                private_mode not in {0o600, 0o640}
                or info.st_gid != expected_gid
            )
        )
    ):
        raise ContractError("源文件类型、所有者或权限异常")


def resolve_source(current):
    current_info = current.lstat()
    releases = current.parent / "releases"
    releases_info = releases.lstat()
    if (
        current.name != "current"
        or not stat.S_ISLNK(current_info.st_mode)
        or current_info.st_uid != os.geteuid()
        or not stat.S_ISDIR(releases_info.st_mode)
        or stat.S_ISLNK(releases_info.st_mode)
        or releases_info.st_uid != os.geteuid()
        or releases_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ContractError("current 或 releases 边界异常")
    raw = Path(os.readlink(current))
    raw = raw if raw.is_absolute() else current.parent / raw
    raw_info = raw.lstat()
    source = current.resolve(strict=True)
    if (
        not stat.S_ISDIR(raw_info.st_mode)
        or stat.S_ISLNK(raw_info.st_mode)
        or source.parent != releases.resolve(strict=True)
        or raw.resolve(strict=True) != source
        or re.fullmatch(r"[A-Za-z0-9._-]+", source.name) is None
    ):
        raise ContractError("current 未直达 releases 下的单层目录")
    for directory in (
        source,
        source / "app",
        source / "venv",
        source / "private-metadata",
    ):
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ContractError("源 release 目录边界异常")
    return source


def stat_identity(info, kind, link_target=None):
    return (
        kind,
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        link_target,
    )


def require_safe_tree_entry(path, info, trusted_uid):
    if info.st_uid != trusted_uid:
        raise ContractError(f"venv 树存在非受信所有者: {path}")
    if stat.S_ISDIR(info.st_mode):
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ContractError(f"venv 目录可被 group/other 写入: {path}")
        return "directory"
    if stat.S_ISREG(info.st_mode):
        if (
            info.st_nlink != 1
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ContractError(f"venv 普通文件硬链接或权限异常: {path}")
        return "file"
    if stat.S_ISLNK(info.st_mode):
        if info.st_nlink != 1:
            raise ContractError(f"venv 符号链接硬链接异常: {path}")
        return "symlink"
    raise ContractError(f"venv 含 FIFO、socket、device 或异常类型: {path}")


def require_safe_external_directory(path, info, trusted_uid):
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid not in {0, trusted_uid}
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ContractError(f"venv 外部 Python 父目录边界异常: {path}")


def validate_system_python_chain(entry, trusted_uid):
    """只解析受信系统 Python 链；任何可写父目录或 '..' 都 fail closed。"""
    entry = Path(entry)
    if not entry.is_absolute() or ".." in entry.parts:
        raise ContractError("系统 Python 入口路径异常")
    current = entry
    visited = set()
    evidence = []
    for _hop in range(16):
        state = str(current)
        if state in visited:
            raise ContractError("系统 Python 符号链接链循环")
        visited.add(state)

        directory = Path("/")
        root_info = directory.lstat()
        require_safe_external_directory(directory, root_info, trusted_uid)
        evidence.append(
            ("directory", str(directory), stat_identity(
                root_info, "directory"
            ))
        )
        for part in current.parent.parts[1:]:
            directory /= part
            directory_info = directory.lstat()
            require_safe_external_directory(
                directory, directory_info, trusted_uid
            )
            evidence.append(
                (
                    "directory",
                    str(directory),
                    stat_identity(directory_info, "directory"),
                )
            )

        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in {0, trusted_uid} or info.st_nlink != 1:
                raise ContractError("系统 Python 符号链接所有者或硬链接异常")
            raw_target = os.readlink(current)
            target = Path(raw_target)
            if (
                not raw_target
                or "." in target.parts
                or ".." in target.parts
            ):
                raise ContractError("系统 Python symlink target 含禁止组件")
            evidence.append(
                (
                    "symlink",
                    str(current),
                    stat_identity(info, "symlink", raw_target),
                )
            )
            current = target if target.is_absolute() else current.parent / target
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != trusted_uid
            or info.st_nlink != 1
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not info.st_mode & stat.S_IXUSR
        ):
            raise ContractError("系统 Python 最终解释器边界异常")
        evidence.append(
            ("file", str(current), stat_identity(info, "file"))
        )
        return current, tuple(evidence)
    raise ContractError("系统 Python 符号链接层数过多")


def standard_python_identity(
    venv, trusted_uid, helper_python_entry, expected_minor
):
    directory_evidence = []
    for directory in (venv, venv / "bin"):
        info = directory.lstat()
        if require_safe_tree_entry(
            directory, info, trusted_uid
        ) != "directory":
            raise ContractError("venv Python 父目录边界异常")
        directory_evidence.append(
            (str(directory), stat_identity(info, "directory"))
        )
    expected_links = {
        venv / "bin" / "python": "python3",
        venv / "bin" / "python3": str(helper_python_entry),
        venv / "bin" / f"python{expected_minor}": "python3",
    }
    link_evidence = []
    for path, expected_target in expected_links.items():
        info = path.lstat()
        if (
            not stat.S_ISLNK(info.st_mode)
            or info.st_uid != trusted_uid
            or info.st_nlink != 1
        ):
            raise ContractError("venv Python 标准链接类型或所有者异常")
        raw_target = os.readlink(path)
        target_parts = Path(raw_target).parts
        if (
            raw_target != expected_target
            or "." in target_parts
            or ".." in target_parts
        ):
            raise ContractError("venv Python 标准链接目标异常")
        link_evidence.append(
            (str(path), stat_identity(info, "symlink", raw_target))
        )
    system_python, system_evidence = validate_system_python_chain(
        helper_python_entry, trusted_uid
    )
    return (
        "system",
        str(system_python),
        tuple(directory_evidence),
        tuple(link_evidence),
        system_evidence,
    )


def validate_venv_tree(
    venv, trusted_uid, helper_python_entry, expected_minor
):
    """无跟随扫描整个 venv，并返回可用于复制前后复核的身份快照。"""
    root_info = venv.lstat()
    if require_safe_tree_entry(venv, root_info, trusted_uid) != "directory":
        raise ContractError("venv 根不是受信目录")

    identities = {
        ".": stat_identity(root_info, "directory"),
    }
    portable = {
        ".": ("directory", stat.S_IMODE(root_info.st_mode)),
    }
    symlinks = []
    pending = [venv]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ContractError("venv 目录无法无跟随扫描") from exc
        for entry in entries:
            item = directory / entry.name
            relative = item.relative_to(venv).as_posix()
            info = entry.stat(follow_symlinks=False)
            kind = require_safe_tree_entry(item, info, trusted_uid)
            if kind == "directory":
                identities[relative] = stat_identity(info, kind)
                portable[relative] = (kind, stat.S_IMODE(info.st_mode))
                pending.append(item)
            elif kind == "file":
                identities[relative] = stat_identity(info, kind)
                portable[relative] = (
                    kind,
                    stat.S_IMODE(info.st_mode),
                    info.st_size,
                )
            else:
                try:
                    link_target = os.readlink(item)
                except OSError as exc:
                    raise ContractError("venv 符号链接读取失败") from exc
                identities[relative] = stat_identity(
                    info, kind, link_target
                )
                portable[relative] = (kind, link_target)
                symlinks.append((item, relative))

    allowed_symlinks = {
        "bin/python",
        "bin/python3",
        f"bin/python{expected_minor}",
        "lib64",
    }
    for item, relative in symlinks:
        if relative not in allowed_symlinks:
            raise ContractError("venv 含标准链之外的符号链接")
        if relative == "lib64":
            if os.readlink(item) != "lib":
                raise ContractError("venv lib64 链接目标异常")
            lib_info = (venv / "lib").lstat()
            if require_safe_tree_entry(
                venv / "lib", lib_info, trusted_uid
            ) != "directory":
                raise ContractError("venv lib64 目标不是受信目录")

    interpreter_identity = standard_python_identity(
        venv, trusted_uid, helper_python_entry, expected_minor
    )
    interpreter_info = Path(interpreter_identity[1]).lstat()
    interpreter_portable = (
        "system",
        interpreter_identity[1],
        stat.S_IMODE(interpreter_info.st_mode),
        interpreter_info.st_size,
    )

    # 扫描末尾再次核对根身份，捕获遍历期间的根替换。
    if stat_identity(venv.lstat(), "directory") != identities["."]:
        raise ContractError("venv 根在扫描期间发生变化")
    return {
        "identity": tuple(sorted(identities.items())),
        "portable": tuple(sorted(portable.items())),
        "interpreter_identity": interpreter_identity,
        "interpreter_portable": interpreter_portable,
    }


def require_interpreter_snapshot(
    snapshot, venv, trusted_uid, helper_python_entry, expected_minor
):
    current_identity = standard_python_identity(
        venv, trusted_uid, helper_python_entry, expected_minor
    )
    if current_identity != snapshot["interpreter_identity"]:
        raise ContractError("venv Python 在执行前身份发生变化")


def require_same_source_snapshot(before, after, phase):
    if (
        before["identity"] != after["identity"]
        or before["interpreter_identity"] != after["interpreter_identity"]
    ):
        raise ContractError(f"源 venv 在{phase}前后发生变化")


def require_matching_copy(source_snapshot, target_snapshot):
    if (
        source_snapshot["portable"] != target_snapshot["portable"]
        or source_snapshot["interpreter_portable"]
        != target_snapshot["interpreter_portable"]
    ):
        raise ContractError("候选 venv 与复制前源树不一致")


def cleanup_created_target(target):
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.geteuid()
    ):
        shutil.rmtree(target)


def write_receipts(
    metadata, lock_sha, expected_minor, method, source_id, source_inspect_sha,
    inspect_bytes, python_version,
):
    try:
        metadata_info = metadata.lstat()
    except FileNotFoundError:
        metadata.mkdir(mode=0o700, parents=True)
        metadata_info = metadata.lstat()
    if (
        not stat.S_ISDIR(metadata_info.st_mode)
        or stat.S_ISLNK(metadata_info.st_mode)
        or metadata_info.st_uid != os.geteuid()
        or metadata_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ContractError("候选私有元数据目录边界异常")
    receipt = {
        "method": method,
        "pip_inspect_sha256": hashlib.sha256(inspect_bytes).hexdigest(),
        "python_major_minor": expected_minor,
        "python_version": python_version,
        "requirements_lock_sha256": lock_sha,
        "schema_version": 1,
        "source_pip_inspect_sha256": source_inspect_sha,
        "source_release_id": source_id,
    }
    payloads = {
        "python-version.txt": f"{python_version}\n".encode(),
        "requirements-lock.sha256": f"{lock_sha}\n".encode(),
        "pip-inspect.json": inspect_bytes,
        "dependency-receipt.json": (
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    }
    created = []
    try:
        for name, payload in payloads.items():
            destination = metadata / name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(destination, flags, 0o600)
            created.append(destination)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
    except BaseException:
        for destination in created:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        raise


mode = sys.argv[1]
new_lock = Path(sys.argv[2])
target_venv = Path(sys.argv[3])
metadata = Path(sys.argv[4])
lock_sha = sys.argv[5]
expected_minor = sys.argv[6]
if hashlib.sha256(new_lock.read_bytes()).hexdigest() != lock_sha:
    raise SystemExit(65)

if mode == "reuse":
    current = Path(sys.argv[7])
    copy_started = False
    try:
        source = resolve_source(current)
        trusted_uid = os.geteuid()
        source_venv = source / "venv"
        helper_python_entry = Path(sys.executable)
        if not helper_python_entry.is_absolute():
            raise ContractError("依赖 helper Python 入口不是绝对路径")
        try:
            helper_python_entry.relative_to(source_venv)
        except ValueError:
            pass
        else:
            raise ContractError("依赖 helper 不得由 source venv 启动")
        trusted_system_python, _helper_evidence = (
            validate_system_python_chain(helper_python_entry, trusted_uid)
        )
        # 任何 source venv 代码执行前，先完成全树无跟随信任边界校验。
        source_snapshot = validate_venv_tree(
            source_venv,
            trusted_uid,
            helper_python_entry,
            expected_minor,
        )
        source_lock = source / "app" / "requirements.lock"
        source_meta = source / "private-metadata"
        source_python = source_venv / "bin" / "python"
        source_gid = source_meta.lstat().st_gid
        for path, private in (
            (source_lock, False),
            (source_meta / "requirements-lock.sha256", True),
            (source_meta / "python-version.txt", True),
            (source_meta / "pip-inspect.json", True),
        ):
            require_safe_file(path, private, source_gid)
        if hashlib.sha256(source_lock.read_bytes()).hexdigest() != lock_sha:
            raise ContractError("源锁摘要不一致")
        if (source_meta / "requirements-lock.sha256").read_text().splitlines() \
            != [lock_sha]:
            raise ContractError("源锁摘要收据不一致")
        recorded_inspect = (source_meta / "pip-inspect.json").read_bytes()
        if inspect_packages(json.loads(recorded_inspect)) != lock_packages(new_lock):
            raise ContractError("源 pip inspect 收据与锁不一致")
        if f"{sys.version_info.major}.{sys.version_info.minor}" != expected_minor:
            raise ContractError("系统 Python major.minor 不一致")
        source_interpreter_guard = lambda: require_interpreter_snapshot(
            source_snapshot,
            source_venv,
            trusted_uid,
            helper_python_entry,
            expected_minor,
        )
        live_inspect, python_version = live_contract(
            source_python,
            source_lock,
            expected_minor,
            before_exec=source_interpreter_guard,
        )
        if (source_meta / "python-version.txt").read_text().splitlines() \
            != [python_version]:
            raise ContractError("源 Python 版本收据不一致")
        if resolve_source(current) != source:
            raise ContractError("current 在源核验期间发生变化")
        pre_copy_snapshot = validate_venv_tree(
            source_venv,
            trusted_uid,
            helper_python_entry,
            expected_minor,
        )
        require_same_source_snapshot(
            source_snapshot, pre_copy_snapshot, "代码核验"
        )
        del source_interpreter_guard
        del source_snapshot
        copy_started = True
        shutil.copytree(
            source_venv,
            target_venv,
            symlinks=True,
            copy_function=shutil.copy2,
        )
        if resolve_source(current) != source:
            raise ContractError("current 在复制期间发生变化")
        post_copy_snapshot = validate_venv_tree(
            source_venv,
            trusted_uid,
            helper_python_entry,
            expected_minor,
        )
        require_same_source_snapshot(
            pre_copy_snapshot, post_copy_snapshot, "复制"
        )
        del post_copy_snapshot
        target_snapshot = validate_venv_tree(
            target_venv,
            trusted_uid,
            helper_python_entry,
            expected_minor,
        )
        require_matching_copy(pre_copy_snapshot, target_snapshot)
        del pre_copy_snapshot
        target_interpreter_guard = lambda: require_interpreter_snapshot(
            target_snapshot,
            target_venv,
            trusted_uid,
            helper_python_entry,
            expected_minor,
        )
        new_inspect, new_python_version = live_contract(
            target_venv / "bin" / "python",
            new_lock,
            expected_minor,
            before_exec=target_interpreter_guard,
        )
        del target_interpreter_guard
        del target_snapshot
        write_receipts(
            metadata,
            lock_sha,
            expected_minor,
            "verified-current-clone",
            source.name,
            hashlib.sha256(recorded_inspect).hexdigest(),
            new_inspect,
            new_python_version,
        )
    except ContractError as exc:
        print(f"当前 release 不可安全复用: {exc}", file=sys.stderr)
        if copy_started:
            cleanup_created_target(target_venv)
        raise SystemExit(65 if copy_started else 75)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"当前 release 复用证据异常: {type(exc).__name__}", file=sys.stderr)
        if copy_started:
            cleanup_created_target(target_venv)
        raise SystemExit(65 if copy_started else 75)
elif mode == "finalize":
    inspect_bytes, python_version = live_contract(
        target_venv / "bin" / "python", new_lock, expected_minor
    )
    write_receipts(
        metadata,
        lock_sha,
        expected_minor,
        "fresh-install",
        None,
        None,
        inspect_bytes,
        python_version,
    )
else:
    raise SystemExit(64)
PY
}

if [ "$INSTALL_MODE" = "reuse-current" ]; then
    run_dependency_helper \
        reuse \
        "$LOCK_FILE" \
        "$RELEASE_VENV" \
        "$METADATA_DIR" \
        "$ACTUAL_LOCK_SHA" \
        "$EXPECTED_PYTHON_MAJOR_MINOR" \
        "$CURRENT_LINK"
    trap - EXIT
    exit 0
fi

FRESH_VENV_STARTED=1
python3 -m venv "$RELEASE_VENV"
"$RELEASE_VENV/bin/python" -m pip install \
    --index-url https://pypi.org/simple \
    --no-cache-dir \
    --no-compile \
    --require-hashes \
    --only-binary=:all: \
    -r "$LOCK_FILE"
run_dependency_helper \
    finalize \
    "$LOCK_FILE" \
    "$RELEASE_VENV" \
    "$METADATA_DIR" \
    "$ACTUAL_LOCK_SHA" \
    "$EXPECTED_PYTHON_MAJOR_MINOR"
FRESH_VENV_STARTED=0
trap - EXIT
