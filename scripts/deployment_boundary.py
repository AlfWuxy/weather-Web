#!/usr/bin/env python3
"""发布传输与公网验收边界；只读取主机、服务及网络证据。"""
from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import socket
import ssl
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


class BoundaryError(ValueError):
    pass


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise BoundaryError("只读边界命令失败，拒绝推定安全")
    return result.stdout


def trusted_file(path):
    path = Path(path).expanduser()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
        raise BoundaryError("SSH 信任文件须为可信账号持有且不可由其他账号修改的普通文件")
    return path


def validate_option(key, value):
    key, value = key.lower(), value.strip()
    if key == "stricthostkeychecking" and value.lower() not in {"yes", "true"}:
        raise BoundaryError("SSH 必须固定 StrictHostKeyChecking=yes")
    if key in {"verifyhostkeydns", "updatehostkeys"} and value.lower() not in {"no", "false"}:
        raise BoundaryError("SSH 主机信任必须来自预置 known_hosts")
    if key == "knownhostscommand" and value.lower() != "none":
        raise BoundaryError("部署不接受动态 KnownHostsCommand")
    if key in {"userknownhostsfile", "globalknownhostsfile"}:
        paths = shlex.split(value)
        if not paths or any(p.lower() in {"none", "null", "off", "no", "/dev/null"} for p in paths):
            raise BoundaryError("known_hosts 不得为空或指向禁用目标")
        for path in paths:
            trusted_file(path)


def ssh_arguments(options, target, control_dir):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9_.:-]+", target):
        raise BoundaryError("SSH 部署目标格式无效")
    supplied = shlex.split(options)
    index = 0
    while index < len(supplied):
        item = supplied[index]
        if item == "-o" or item.startswith("-o"):
            if item == "-o":
                index += 1
                if index >= len(supplied):
                    raise BoundaryError("SSH 选项缺少值")
                setting = supplied[index]
            else:
                setting = item[2:]
            parts = re.split(r"[=\s]+", setting, maxsplit=1)
            if len(parts) != 2:
                raise BoundaryError("SSH -o 选项格式无效")
            validate_option(*parts)
        elif item in {"-p", "-i", "-J", "-F"} or item[:2] in {"-p", "-i", "-J", "-F"}:
            if len(item) == 2:
                index += 1
                if index >= len(supplied):
                    raise BoundaryError("SSH 选项缺少值")
                value = supplied[index]
            else:
                value = item[2:]
            if item[:2] == "-F":
                trusted_file(value)
        elif item not in {"-4", "-6", "-C", "-q", "-v", "-vv", "-vvv"}:
            raise BoundaryError("SSH 选项不是部署允许的连接选项")
        index += 1
    directory = Path(control_dir)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise BoundaryError("SSH 本轮控制目录必须由执行账号独占")
    # OpenSSH 使用第一个同名选项；同时拒绝显式降级，避免静默忽略不安全配置。
    fixed = ["-o", "StrictHostKeyChecking=yes", "-o", "VerifyHostKeyDNS=no", "-o", "UpdateHostKeys=no",
             "-o", "KnownHostsCommand=none", "-o", f"ControlPath={directory}/ssh"]
    args = fixed + supplied
    effective = {}
    for line in run(["ssh", *args, "-G", target]).splitlines():
        key, _, value = line.partition(" ")
        effective[key] = value.strip()
    if effective.get("stricthostkeychecking") not in {"yes", "true"}:
        raise BoundaryError("SSH 有效配置未启用严格校验")
    host = effective.get("hostkeyalias") or effective.get("hostname")
    port = effective.get("port")
    if not host or not port or not port.isdigit():
        raise BoundaryError("无法确定 SSH 有效主机及端口")
    lookup = host if port == "22" else f"[{host}]:{port}"
    pinned = False
    for key in ("userknownhostsfile", "globalknownhostsfile"):
        paths = shlex.split(effective.get(key, ""))
        if not paths or any(p.lower() in {"none", "null", "off", "no", "/dev/null"} for p in paths):
            raise BoundaryError("SSH 有效 known_hosts 配置已禁用")
        for name in paths:
            path = Path(name).expanduser()
            if not path.exists():
                continue
            trusted_file(path)
            result = subprocess.run(["ssh-keygen", "-F", lookup, "-f", str(path)], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and any(line and not line.startswith("#") for line in result.stdout.splitlines()):
                pinned = True
    if not pinned:
        raise BoundaryError("目标缺少可信预置主机键；请先通过独立渠道核对并登记")
    return args


def public_origin(value):
    parsed = urllib.parse.urlsplit(value)
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        pass
    else:
        raise BoundaryError("公网入口须为证书覆盖的域名")
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)):
        raise BoundaryError("公网入口必须是无附加参数的 HTTPS origin")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verify_https(origin):
    origin = public_origin(origin)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(origin + "/healthz", headers={"Cache-Control": "no-cache", "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; CaseWeatherDeploy/1.0)"})
    with opener.open(request, timeout=15) as response:
        body = response.read(4097)
        if (response.status != 200 or response.headers.get_content_type() != "application/json"
                or "no-store" not in response.headers.get("Cache-Control", "").lower()
                or len(body) > 4096 or json.loads(body) != {"status": "ok"}):
            raise BoundaryError("公网健康响应不符合本应用契约")



def verify_http_redirect(origin):
    origin = public_origin(origin)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(origin.replace("https:", "http:", 1) + "/healthz", method="HEAD",
        headers={"User-Agent": "Mozilla/5.0 (compatible; CaseWeatherDeploy/1.0)"})
    try:
        with opener.open(request, timeout=15):
            raise BoundaryError("公网 HTTP 没有强制转向 HTTPS")
    except urllib.error.HTTPError as response:
        try:
            if response.code not in {301, 302, 307, 308} or response.headers.get("Location") != origin + "/healthz":
                raise BoundaryError("公网 HTTP 跳转证据未知或目标异常")
        finally:
            response.close()

def process_uid(pid):
    source = Path(f"/proc/{pid}/status").read_text()
    match = re.search(r"^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", source, re.M)
    if not match or len(set(match.groups())) != 1 or int(match[1]) == 0:
        raise BoundaryError("应用进程实际 UID 未证明为非 root")
    return int(match[1])


def origin_evidence():
    values = {}
    for line in run(["systemctl", "show", "case-weather.service", "--property=MainPID,User,ActiveState"]).splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    uid = pwd.getpwnam("case-weather").pw_uid
    pid = int(values.get("MainPID", "0"))
    if uid == 0 or pid <= 0 or values.get("ActiveState") != "active" or values.get("User") != "case-weather" or process_uid(pid) != uid:
        raise BoundaryError("应用活动进程与专用账号不一致")
    listeners = {5000: [], 8080: []}
    all_rows = []
    # 分别执行 IPv4/IPv6 查询，任一查询失败都不能宣称已检查全部地址族。
    for family in ("-4", "-6"):
        for line in run(["ss", family, "-H", "-ltnp"]).splitlines():
            fields = line.split()
            if len(fields) < 5:
                raise BoundaryError("无法解析完整监听证据")
            address, _, port_text = fields[3].rpartition(":")
            address = address.strip("[]").split("%", 1)[0]
            port = int(port_text)
            pids = {int(value) for value in re.findall(r"pid=(\d+)", line)}
            loopback = ipaddress.ip_address(address).is_loopback if address != "*" else False
            row = (port, loopback, pids)
            all_rows.append(row)
            if port in listeners:
                if not loopback or not pids:
                    raise BoundaryError("应用或边缘代理端口未限定在已知进程的回环监听")
                listeners[port].append(row)
    if not all(listeners.values()) or not any(pid in row[2] for row in listeners[5000]):
        raise BoundaryError("缺少应用及边缘代理实际回环监听证据")
    app_pids = set().union(*(row[2] for row in listeners[5000]))
    if any(process_uid(item) != uid for item in app_pids):
        raise BoundaryError("应用监听进程实际账号异常")
    proxy_pids = set().union(*(row[2] for row in listeners[8080]))
    if any(not loopback and pids & (app_pids | proxy_pids) for _, loopback, pids in all_rows):
        raise BoundaryError("同一应用或代理进程还存在公网监听")
    addresses = set()
    for interface in json.loads(run(["ip", "-j", "address", "show"])):
        for entry in interface.get("addr_info", []):
            address = ipaddress.ip_address(entry["local"])
            if address.is_global:
                addresses.add(str(address))
    if not addresses:
        raise BoundaryError("未能枚举源站公网地址，NAT 或未知拓扑需先提供可核验路径")
    return {"schema": 1, "uid": uid, "pid": pid, "loopback_ports": [5000, 8080], "families": [4, 6], "addresses": sorted(addresses)}


def verify_origin_ports(evidence):
    if (evidence.get("schema") != 1 or type(evidence.get("uid")) is not int or evidence["uid"] <= 0
            or type(evidence.get("pid")) is not int or evidence["pid"] <= 0
            or evidence.get("loopback_ports") != [5000, 8080] or evidence.get("families") != [4, 6]
            or not evidence.get("addresses")):
        raise BoundaryError("源站运行证据不完整")
    for value in evidence["addresses"]:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise BoundaryError("源站公网地址证据无效")
        for port in (5000, 8080):
            try:
                with socket.create_connection((str(address), port), timeout=4):
                    raise BoundaryError("源站应用端口可以从外部直接连接")
            except (ConnectionRefusedError, TimeoutError):
                pass
            except OSError as error:
                if error.errno != errno.ECONNREFUSED:
                    raise BoundaryError("外部端口探测网络不可达或结果未知") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    ssh = sub.add_parser("ssh-options")
    ssh.add_argument("--target", required=True)
    ssh.add_argument("--control-dir", required=True)
    sub.add_parser("ssh-rsh")
    public = sub.add_parser("public")
    public.add_argument("--origin", required=True)
    public.add_argument("--with-origin-evidence", action="store_true")
    sub.add_parser("origin")
    args = parser.parse_args()
    if args.mode == "ssh-options":
        values = ssh_arguments(sys.stdin.read(65537), args.target, args.control_dir)
        sys.stdout.buffer.write(b"\0".join(value.encode() for value in values) + b"\0")
    elif args.mode == "ssh-rsh":
        values = sys.stdin.buffer.read().decode().rstrip("\0").split("\0")
        print(shlex.join(["ssh", *values]))
    elif args.mode == "origin":
        print(json.dumps(origin_evidence(), sort_keys=True))
    else:
        verify_https(args.origin)
        verify_http_redirect(args.origin)
        if args.with_origin_evidence:
            verify_origin_ports(json.load(sys.stdin))
        print("公网 HTTPS 与所请求的源站边界核验通过")


if __name__ == "__main__":
    try:
        main()
    except (BoundaryError, OSError, ValueError, KeyError, subprocess.SubprocessError, urllib.error.URLError):
        print("部署安全边界证据缺失或校验失败，拒绝宣称发布完成", file=sys.stderr)
        raise SystemExit(64)
