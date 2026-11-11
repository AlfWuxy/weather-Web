#!/usr/bin/env python3
"""校验部署的主机信任、TLS 反代、非特权身份与源站端口边界。"""
import ipaddress
import errno
import json
import os
import pwd
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
from urllib.parse import urlsplit
import urllib.request


def public_hostname(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or not parsed.hostname or not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname)):
        raise ValueError("PUBLIC_BASE_URL 必须使用 HTTPS 域名与标准端口，不支持明文豁免")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return parsed.hostname
    raise ValueError("PUBLIC_BASE_URL 必须是证书对应的域名")


def known_hosts(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0 or info.st_mode & 0o022:
        raise ValueError("known_hosts 必须预先通过可信渠道配置，且不能被其他用户写入")
    if info.st_uid not in (0, os.getuid()):
        raise ValueError("known_hosts 属主不可信")


_PYTHON_LINK_NAME = re.compile(r"^python(\d+(\.\d+)?)?$")
_WRITABLE_DIR_NAMES = frozenset({"instance", "storage", "logs"})


def _is_venv_dirname(part):
    # 只接受 .venv / .venv2 / .venv3，拒绝 .venv_evil 这种前缀伪装。
    return part == ".venv" or re.fullmatch(r"\.venv\d*", part) is not None


def _inside_venv(root, path):
    try:
        relative = Path(path).relative_to(root)
    except ValueError:
        return False
    # 只认项目根的直接子目录，拒绝更深层的 .venv*。
    if len(relative.parts) < 2 or any(part in {".", ".."} for part in relative.parts):
        return False
    part = relative.parts[0]
    return part == ".venv" or re.fullmatch(r"\.venv\d*", part) is not None


def _under_writable_project_dir(root, candidate):
    """解析后的路径落在项目内 instance、storage 或 logs 时视为可写目录。"""
    writable = {name.casefold() for name in _WRITABLE_DIR_NAMES}
    bases = [Path(os.path.abspath(root))]
    candidates = [Path(os.path.normpath(os.path.abspath(candidate)))]
    try:
        bases.append(Path(root).resolve(strict=False))
        candidates.append(Path(candidate).resolve(strict=False))
    except (OSError, RuntimeError):
        return True
    for base, target in zip(bases, candidates):
        try:
            relative = target.relative_to(base)
        except ValueError:
            continue
        if relative.parts and relative.parts[0].casefold() in writable:
            return True
    return False


def _trusted_python_prefixes(project):
    """绝对解释器只能落在 /usr、/Library 或当前系统 Python 安装前缀内。"""
    candidates = [Path("/usr"), Path("/Library")]
    base = getattr(sys, "base_prefix", "") or ""
    if base:
        candidates.append(Path(base))
    for executable in (sys.executable, os.path.realpath(sys.executable)):
        path = Path(executable)
        if not path.is_absolute():
            continue
        candidates.append(path.parent.parent if path.parent.name == "bin" else path.parent)
    try:
        project_root = Path(project).resolve(strict=False)
    except (OSError, RuntimeError):
        project_root = Path(os.path.abspath(project))
    prefixes = []
    for candidate in candidates:
        prefix = Path(os.path.normpath(candidate))
        if not prefix.is_absolute() or prefix == Path("/"):
            continue
        try:
            prefix.resolve(strict=False).relative_to(project_root)
        except ValueError:
            if prefix not in prefixes:
                prefixes.append(prefix)
        except (OSError, RuntimeError):
            continue
    return prefixes


def _interpreter_symlink_target_allowed(root, raw):
    """bin/python* 的目标必须是受信任绝对路径，或不含 .. 的相对路径。"""
    if not raw or "\x00" in raw:
        return False
    if os.path.isabs(raw):
        normalized = os.path.normpath(raw)
        if not os.path.isabs(normalized):
            return False
        target = Path(normalized)
        for prefix in _trusted_python_prefixes(root):
            try:
                target.relative_to(prefix)
            except ValueError:
                continue
            return True
        return False
    return ".." not in Path(raw).parts


def _python_symlink_target_allowed(root, path):
    try:
        raw = os.readlink(path)
    except OSError:
        return False
    if not raw or "\x00" in raw:
        return False
    target = Path(raw)
    if not target.is_absolute():
        target = Path(path).parent / target
    lexical = Path(os.path.normpath(target))
    if _under_writable_project_dir(root, lexical):
        return False
    try:
        # 目标不存在、断链或解析失败都拒绝，避免悬空链接被保留。
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not resolved.exists() or _under_writable_project_dir(root, resolved):
        return False
    # 解析后的绝对路径必须仍在受信前缀内；不含 .. 的相对链接沿用原规则。
    if not (
        _interpreter_symlink_target_allowed(root, os.fspath(resolved))
        or (not os.path.isabs(raw) and ".." not in Path(raw).parts)
    ):
        return False
    try:
        if Path(path).is_dir():
            return False
        info = os.stat(path)
    except OSError:
        return False
    # 目标必须存在，且是常规可执行文件；目录符号链接和其他类型一律拒绝。
    return stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)


def _allowed_venv_symlink(root, path):
    if not _inside_venv(root, path):
        return False
    try:
        relative = Path(path).relative_to(root)
    except ValueError:
        return False
    if not relative.parts or not _is_venv_dirname(relative.parts[0]):
        return False
    venv_name = relative.parts[0]
    if len(relative.parts) == 3:
        link_name = relative.parts[2]
        if relative.parts == (venv_name, "bin", link_name) and _PYTHON_LINK_NAME.fullmatch(link_name):
            try:
                raw = os.readlink(path)
            except OSError:
                return False
            # 绝对目标只允许 /usr、/Library 或系统 Python 前缀；相对目标不得逃出目录。
            return _interpreter_symlink_target_allowed(root, raw) and _python_symlink_target_allowed(root, path)
    if relative.parts == (venv_name, "lib64"):
        try:
            return os.readlink(path) == "lib"
        except OSError:
            return False
    return False


def reject_forbidden_symlinks(project):
    root = Path(project)
    if root.is_symlink():
        raise ValueError("应用目录或运行账号不安全")
    if not root.exists():
        return

    def onerror(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        current = Path(directory)
        for name in list(dirs) + list(files):
            path = current / name
            if not path.is_symlink() or _allowed_venv_symlink(root, path):
                continue
            os.unlink(path)
            raise ValueError(f"发现禁止的符号链接: {path}")


def secure_tree(project, user):
    root = Path(project)
    account = pwd.getpwnam(user)
    if account.pw_uid == 0 or root.is_symlink():
        raise ValueError("应用目录或运行账号不安全")
    reject_forbidden_symlinks(project)
    mutable = {root / name for name in ("instance", "storage", "logs")}
    for directory, dirs, files in os.walk(root, followlinks=False):
        here = Path(directory)
        if here == root:
            dirs[:] = [name for name in dirs if name not in ("instance", "storage", "logs", "backups")]
        os.chown(here, 0, account.pw_gid)
        os.chmod(here, 0o750)
        for name in files:
            path = here / name
            if path.is_symlink():
                continue
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("部署树存在非普通文件或硬链接")
            if name.startswith(".env"):
                owner = account.pw_uid if path == root / ".env" else 0
                os.chown(path, owner, account.pw_gid)
                os.chmod(path, 0o600)
            else:
                os.chown(path, 0, account.pw_gid)
                os.chmod(path, 0o750 if info.st_mode & 0o111 else 0o640)
    for path in mutable:
        if path.is_symlink():
            raise ValueError("可写目录不得是链接")
        for directory, dirs, files in os.walk(path, followlinks=False):
            os.chown(directory, account.pw_uid, account.pw_gid)
            os.chmod(directory, 0o700)
            for name in dirs + files:
                child = Path(directory) / name
                if child.is_symlink() or (child.is_file() and child.stat().st_nlink > 1):
                    raise ValueError("可写目录不得包含链接")
                os.chown(child, account.pw_uid, account.pw_gid)
                os.chmod(child, 0o700 if child.is_dir() else 0o600)


def nginx_config(url, cert, key):
    host = public_hostname(url)
    # /e/<token> 的路径和 query 都是行动链接凭据。只在该 location 关闭访问日志，
    # 避免默认 combined 格式把完整 GET /e/<token> 写入 access_log。
    proxy = """        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_hide_header Strict-Transport-Security;
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;"""
    return f"""# 证书须由运维预置；仅本机回环连接应用。
# /e/ 行动链接的 token 与 query 不进入 access_log。
server {{
    listen 80;
    listen [::]:80;
    server_name {host};
    location /e/ {{
        access_log off;
        return 308 https://{host}$request_uri;
    }}
    location / {{
        return 308 https://{host}$request_uri;
    }}
}}
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {host};
    ssl_certificate {cert};
    ssl_certificate_key {key};
    ssl_protocols TLSv1.2 TLSv1.3;
    location /e/ {{
        access_log off;
{proxy}
    }}
    location / {{
{proxy}
    }}
}}
"""


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def verify_origin(project, user, url, cert, key):
    if user == "root" or not user:
        raise ValueError("应用不得使用 root")
    expected_uid = run("id", "-u", user).strip()
    if expected_uid == "0":
        raise ValueError("应用账号实际 UID 不得为零")
    reject_forbidden_symlinks(project)
    expected = nginx_config(url, cert, key)
    conf = Path("/etc/nginx/conf.d/case-weather.conf")
    if conf.read_text() != expected:
        raise ValueError("HTTPS 反代配置与本次部署不一致")
    loaded = subprocess.run(["nginx", "-T"], capture_output=True, text=True, check=True)
    if expected not in loaded.stdout or "conflicting server name" in loaded.stderr:
        raise ValueError("HTTPS 反代配置未被加载或存在冲突")
    for name in ("case-weather", "case-weather-cache", "case-weather-dispatch", "case-weather-risk-precompute"):
        effective_user = run("systemctl", "show", f"{name}.service", "--property=User", "--value").strip()
        if effective_user != user:
            raise ValueError("服务未使用预期的专用账号")
        # 配置中的User不能证明已经运行的旧进程身份，核对活动PID的真实凭据。
        for prop in ("MainPID", "ControlPID"):
            pid = run("systemctl", "show", f"{name}.service", f"--property={prop}", "--value").strip()
            if not pid.isdecimal():
                raise ValueError("无法读取服务活动PID")
            if name == "case-weather" and prop == "MainPID" and pid == "0":
                raise ValueError("主服务没有活动进程")
            if pid != "0":
                try:
                    process_status = Path(f"/proc/{pid}/status").read_text()
                except FileNotFoundError:
                    if run("systemctl", "show", f"{name}.service", f"--property={prop}", "--value").strip() == "0":
                        continue
                    raise ValueError("服务进程身份检查期间发生变化")
                identities = next((line.split()[1:] for line in process_status.splitlines() if line.startswith("Uid:")), [])
                if len(identities) != 4 or set(identities) != {expected_uid}:
                    raise ValueError("服务实际进程身份不是专用非root账号")
        for prop, expected_value in (("ProtectSystem", "strict"), ("ProtectHome", "yes"), ("NoNewPrivileges", "yes"), ("UMask", "0077")):
            if run("systemctl", "show", f"{name}.service", f"--property={prop}", "--value").strip() != expected_value:
                raise ValueError("服务缺少只读文件系统或私有权限隔离")
        writable = set(run("systemctl", "show", f"{name}.service", "--property=ReadWritePaths", "--value").split())
        if writable != {str(Path(project) / part) for part in ("instance", "storage", "logs")}:
            raise ValueError("服务可写目录超出明确允许范围")
    listeners = run("ss", "-H", "-ltn", "sport = :5000").splitlines()
    if not listeners or any(line.split()[3] not in ("127.0.0.1:5000", "[::1]:5000") for line in listeners):
        raise ValueError("5000 端口必须且只能监听回环地址")
    # 直接连接源站本地 TLS，并用公开域名做 SNI 与证书主机名校验。
    context = ssl.create_default_context()
    with socket.create_connection(("127.0.0.1", 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=public_hostname(url)) as tls:
            tls.sendall(f"GET /healthz HTTP/1.1\r\nHost: {public_hostname(url)}\r\nConnection: close\r\n\r\n".encode())
            if not tls.recv(1024).startswith(b"HTTP/1.1 200 "):
                raise ValueError("源站 HTTPS 健康检查失败")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_public(url, origin):
    public_hostname(url)
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(url.rstrip("/") + "/healthz", timeout=15) as response:
        if response.status != 200 or json.load(response).get("status") != "ok":
            raise ValueError("公网 HTTPS 健康检查失败")
    addresses = socket.getaddrinfo(origin, 5000, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("未解析到可验证的公网源站地址")
    for family, kind, protocol, _, address in addresses:
        if not ipaddress.ip_address(address[0]).is_global:
            raise ValueError("端口隔离检查必须使用实际公网源站地址")
        with socket.socket(family, kind, protocol) as connection:
            connection.settimeout(3)
            result = connection.connect_ex(address)
            if result == 0:
                raise ValueError("公网可连接源站 5000 端口，拒绝宣称部署成功")
            if result not in (errno.ECONNREFUSED, errno.ETIMEDOUT):
                raise ValueError("源站端口隔离检查未得到可判断结果")


def main():
    mode, *args = sys.argv[1:]
    if mode == "validate-url": public_hostname(*args)
    elif mode == "known-hosts": known_hosts(*args)
    elif mode == "nginx-config": print(nginx_config(*args), end="")
    elif mode == "secure-tree": secure_tree(*args)
    elif mode == "verify-origin": verify_origin(*args)
    elif mode == "verify-public": verify_public(*args)
    else: raise ValueError("未知部署安全检查")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"部署安全检查失败：{exc}" if isinstance(exc, ValueError) else f"部署安全检查失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
