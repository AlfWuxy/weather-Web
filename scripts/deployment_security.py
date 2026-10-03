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


def secure_tree(project, user):
    root = Path(project)
    account = pwd.getpwnam(user)
    if account.pw_uid == 0 or root.is_symlink():
        raise ValueError("应用目录或运行账号不安全")
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
    return f"""# 证书须由运维预置；仅本机回环连接应用。
server {{
    listen 80;
    listen [::]:80;
    server_name {host};
    return 308 https://{host}$request_uri;
}}
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {host};
    ssl_certificate {cert};
    ssl_certificate_key {key};
    ssl_protocols TLSv1.2 TLSv1.3;
    location / {{
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
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
