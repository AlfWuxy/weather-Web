"""用临时目录和进程桩验证部署边界，不访问真实主机。"""
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_secret_write_restricts_existing_and_temporary_files_before_writing(tmp_path, monkeypatch):
    helper = module("secure_environment")
    target = tmp_path / ".env"
    target.write_text("SECRET_KEY=old\nOTHER=retained\n")
    target.chmod(0o666)
    original = helper.tempfile.mkstemp
    observed = []

    def mkstemp(**kwargs):
        fd, name = original(**kwargs)
        observed.append((target.stat().st_mode & 0o777, os.fstat(fd).st_mode & 0o777))
        return fd, name

    monkeypatch.setattr(helper.tempfile, "mkstemp", mkstemp)
    helper.write_values(target, {"SECRET_KEY": "new-value"})
    assert observed == [(0o600, 0o600)]
    assert target.stat().st_mode & 0o777 == 0o600
    assert "OTHER=retained" in target.read_text()
    assert "SECRET_KEY=new-value" in target.read_text()
    assert not list(tmp_path.glob(".env-private-*"))


def test_quick_fix_creates_private_secrets_and_preserves_existing_values(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("quick_fix.sh", "secure_environment.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    first = subprocess.run(["bash", str(scripts / "quick_fix.sh")], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    env = tmp_path / ".env"
    before = env.read_text()
    assert "SECRET_KEY=" in before and "PAIR_TOKEN_PEPPER=" in before
    assert env.stat().st_mode & 0o777 == 0o600
    env.chmod(0o666)
    second = subprocess.run(["bash", str(scripts / "quick_fix.sh")], capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert env.read_text() == before
    assert env.stat().st_mode & 0o777 == 0o600
    for line in before.splitlines():
        assert line.partition("=")[2] not in first.stdout + second.stdout


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_env_links_are_rejected_without_changing_target(tmp_path, kind):
    helper = module("secure_environment")
    real = tmp_path / "unrelated"
    real.write_text("PRIVATE=retained\n")
    real.chmod(0o644)
    alias = tmp_path / ".env"
    alias.symlink_to(real) if kind == "symlink" else os.link(real, alias)
    with pytest.raises((OSError, ValueError)):
        helper.write_values(alias, {"SECRET_KEY": "new-value"})
    assert real.read_text() == "PRIVATE=retained\n"
    assert real.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("url", ["http://site.example", "https://127.0.0.1", "https://site.example:5000", "https://user:password@site.example", "https://site.example/path", "https://site.example/?next=http://other.example"])
def test_public_url_rejects_plaintext_and_alternate_authorities(url):
    with pytest.raises(ValueError):
        module("deployment_security").public_hostname(url)


def test_https_proxy_template_fixes_origin_and_redirects_http():
    helper = module("deployment_security")
    text = helper.nginx_config("https://site.example", "/etc/certs/fullchain.pem", "/etc/certs/privkey.pem")
    assert "proxy_pass http://127.0.0.1:5000;" in text
    assert "return 308 https://site.example$request_uri;" in text
    assert "ssl_certificate /etc/certs/fullchain.pem;" in text
    assert "ssl_certificate_key /etc/certs/privkey.pem;" in text


@pytest.mark.parametrize("permissions", [0o666, 0o622])
def test_known_hosts_rejects_writable_trust_store(tmp_path, permissions):
    helper = module("deployment_security")
    path = tmp_path / "known_hosts"
    path.write_text("trusted-host-key\n")
    path.chmod(permissions)
    with pytest.raises(ValueError):
        helper.known_hosts(path)
    path.chmod(0o600)
    helper.known_hosts(path)


def deploy_fixture(tmp_path, monkeypatch):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "calls.jsonl"
    payload = tmp_path / "payload.json"
    python = shutil.which("python3")
    for name in ("ssh", "rsync"):
        (binaries / name).write_text(
            f"#!{sys.executable}\nimport json,os,sys\n"
            "with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv)+'\\n')\n"
            "if ' merge ' in ' '.join(sys.argv):\n"
            " with open(os.environ['PAYLOAD'], 'w') as f: f.write(sys.stdin.read())\n"
            "if 'rsync' in sys.argv[0]: sys.exit(int(os.environ.get('RSYNC_STATUS','0')))\n"
            "sys.exit(int(os.environ.get('SSH_STATUS','0')))\n"
        )
        (binaries / name).chmod(0o755)
    (binaries / "python3").write_text(
        f"#!/bin/bash\nif [[ \"${{2:-}}\" == verify-public ]]; then exit 0; fi\nexec '{python}' \"$@\"\n"
    )
    (binaries / "python3").chmod(0o755)
    known = tmp_path / "known_hosts"
    known.write_text("fixture trusted key\n")
    known.chmod(0o600)
    env_file = tmp_path / ".env"
    env_file.write_text("QWEATHER_KEY=fixture-sensitive-value\n")
    for key in ("SSH_OPTS", "DEFAULT_SSH_OPTS", "DEPLOY_PASSWORD", "SSHPASS", "DEPLOY_APP_USER", "QWEATHER_KEY"):
        monkeypatch.delenv(key, raising=False)
    values = {
        "PATH": f"{binaries}:{os.environ['PATH']}", "CALLS": str(log), "PAYLOAD": str(payload),
        "ENV_FILE": str(env_file), "CW_SSH_KNOWN_HOSTS": str(known),
        "DEPLOY_SERVER": "host.example", "DEPLOY_USER": "root", "DEPLOY_PROJECT_DIR": "/srv/example-app",
        "DEPLOY_LOCAL_DIR": str(ROOT), "PUBLIC_BASE_URL": "https://site.example",
        "DEPLOY_ORIGIN_ADDRESS": "origin.example", "DEPLOY_TLS_CERT_FILE": "/etc/certs/fullchain.pem",
        "DEPLOY_TLS_KEY_FILE": "/etc/certs/privkey.pem",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return log, payload


def run_script(name):
    return subprocess.run(["bash", str(ROOT / "scripts" / name)], text=True, capture_output=True)


@pytest.mark.parametrize("key,value", [("PUBLIC_BASE_URL", "http://site.example"), ("DEPLOY_APP_USER", "root"), ("SSH_OPTS", "-o StrictHostKeyChecking=no"), ("DEFAULT_SSH_OPTS", "-o StrictHostKeyChecking=accept-new"), ("CW_SSH_KNOWN_HOSTS", "/dev/null")])
def test_unsafe_deploy_settings_fail_before_ssh(tmp_path, monkeypatch, key, value):
    log, _ = deploy_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv(key, value)
    result = run_script("deploy.sh")
    assert result.returncode != 0
    assert not log.exists()
    assert "部署完成" not in result.stdout


def test_pinned_host_rejection_stops_before_upload(tmp_path, monkeypatch):
    log, _ = deploy_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("SSH_STATUS", "255")
    result = run_script("deploy.sh")
    assert result.returncode == 255
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 1
    assert "StrictHostKeyChecking=yes" in calls[0]
    assert "BatchMode=yes" in calls[0]


def test_deploy_uses_private_stdin_and_nonroot_loopback_units(tmp_path, monkeypatch):
    log, payload = deploy_fixture(tmp_path, monkeypatch)
    result = run_script("deploy.sh")
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "fixture-sensitive-value" not in calls
    assert json.loads(payload.read_text())["QWEATHER_KEY"] == "fixture-sensitive-value"
    assert "User=case-weather" in calls and "User=root" not in calls
    assert "--bind 127.0.0.1:5000" in calls and "--bind 0.0.0.0" not in calls
    assert "ReadWritePaths=/srv/example-app/instance /srv/example-app/storage /srv/example-app/logs" in calls
    assert "ProtectSystem=strict" in calls
    assert "verify-origin" in calls
    assert "OnUnitActiveSec=30min" in calls
    assert "ExecStart=/bin/bash /srv/example-app/scripts/weather_cache_sync.sh" in calls
    transfers = [json.loads(line) for line in calls.splitlines() if "rsync" in json.loads(line)[0]]
    assert len(transfers) == 1
    assert "--exclude=/analysis/" in transfers[0]
    for excluded in (".env*", "backups", "storage", "instance", ".claude", ".superpowers"):
        assert excluded in transfers[0]


def test_sync_upload_failure_never_restarts_service(tmp_path, monkeypatch):
    log, _ = deploy_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("RSYNC_STATUS", "23")
    result = run_script("sync.sh")
    assert result.returncode == 23
    assert "systemctl restart" not in log.read_text()
    assert "同步完成" not in result.stdout


def test_backup_repairs_old_modes_and_keeps_new_archive_private(tmp_path, monkeypatch):
    if not shutil.which("sqlite3"):
        pytest.skip("测试运行时缺少sqlite3")
    root = tmp_path / "project"
    (root / "instance").mkdir(parents=True)
    db = root / "instance" / "health_weather.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table synthetic(value text)")
        conn.execute("insert into synthetic values ('fixture')")
    backups = root / "backups"
    backups.mkdir(mode=0o755)
    old = backups / "older.db.gz"
    old.write_bytes(b"fixture")
    old.chmod(0o644)
    monkeypatch.setenv("PROJECT_DIR", str(root))
    monkeypatch.setenv("ENV_FILE", str(root / "missing.env"))
    monkeypatch.delenv("DATABASE_URI", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    result = run_script("backup.sh")
    assert result.returncode == 0, result.stderr
    assert backups.stat().st_mode & 0o777 == 0o700
    archives = list(backups.glob("*.gz"))
    assert len(archives) == 2
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in archives)


def test_backup_repairs_existing_permissions_even_when_database_is_absent(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o755)
    old = backups / "older.db.gz"
    old.write_bytes(b"fixture")
    old.chmod(0o644)
    monkeypatch.setenv("PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("DATABASE_URI", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    result = subprocess.run(["bash", str(ROOT / "scripts/backup.sh"), "--if-present"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert backups.stat().st_mode & 0o777 == 0o700
    assert old.stat().st_mode & 0o777 == 0o600


def test_secure_tree_limits_writes_and_protects_env_backups(tmp_path, monkeypatch):
    helper = module("deployment_security")
    account = SimpleNamespace(pw_uid=501, pw_gid=502)
    monkeypatch.setattr(helper.pwd, "getpwnam", lambda user: account)
    ownership = {}
    monkeypatch.setattr(helper.os, "chown", lambda path, uid, gid: ownership.__setitem__(Path(path), (uid, gid)))
    for name in ("instance", "storage", "logs"):
        (tmp_path / name).mkdir()
    for name in ("app.py", ".env", ".env.backup", "instance/synthetic.db"):
        (tmp_path / name).write_text("fixture")
    helper.secure_tree(str(tmp_path), "case-weather")
    assert ownership[tmp_path / "app.py"][0] == 0
    assert ownership[tmp_path / "instance/synthetic.db"][0] == 501
    assert ownership[tmp_path / ".env.backup"][0] == 0
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".env.backup").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("violation", ["public-listener", "root-uid", "writable-code", "missing-sandbox", None])
def test_origin_boundary_checks_effective_service_and_socket_state(monkeypatch, violation):
    helper = module("deployment_security")
    url, cert, key = "https://site.example", "/etc/certs/fullchain.pem", "/etc/certs/privkey.pem"
    config = helper.nginx_config(url, cert, key)
    monkeypatch.setattr(helper.Path, "read_text", lambda path: config)
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=config, stderr=""))
    values = {"User": "case-weather", "ProtectSystem": "strict", "ProtectHome": "yes", "NoNewPrivileges": "yes", "UMask": "0077", "ReadWritePaths": "/srv/example/instance /srv/example/storage /srv/example/logs"}
    if violation == "writable-code": values["ReadWritePaths"] += " /srv/example"
    if violation == "missing-sandbox": values["ProtectSystem"] = "no"
    def run(*args):
        if args[0] == "id": return "0" if violation == "root-uid" else "501"
        if args[0] == "ss": return "LISTEN 0 128 " + ("0.0.0.0:5000" if violation == "public-listener" else "127.0.0.1:5000") + " 0.0.0.0:*\n"
        return values[args[3].partition("=")[2]]
    monkeypatch.setattr(helper, "run", run)
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def sendall(self, content): assert b"Host: site.example" in content
        def recv(self, size): return b"HTTP/1.1 200 OK\r\n"
    class Context:
        def wrap_socket(self, raw, server_hostname):
            assert server_hostname == "site.example"
            return Stream()
    monkeypatch.setattr(helper.socket, "create_connection", lambda *args, **kwargs: Stream())
    monkeypatch.setattr(helper.ssl, "create_default_context", Context)
    if violation:
        with pytest.raises(ValueError): helper.verify_origin("/srv/example", "case-weather", url, cert, key)
    else:
        helper.verify_origin("/srv/example", "case-weather", url, cert, key)


def test_public_check_does_not_accept_healthy_tls_with_exposed_origin(monkeypatch):
    helper = module("deployment_security")
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"status":"ok"}'
    class Opener:
        def open(self, *args, **kwargs): return Response()
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect_ex(self, address): return 0
    monkeypatch.setattr(helper.urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(helper.socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 5000))])
    monkeypatch.setattr(helper.socket, "socket", lambda *args: Connection())
    with pytest.raises(ValueError, match="公网可连接"):
        helper.verify_public("https://site.example", "origin.example")
    monkeypatch.setattr(Connection, "connect_ex", lambda *args: errno.ECONNREFUSED)
    helper.verify_public("https://site.example", "origin.example")
    monkeypatch.setattr(Connection, "connect_ex", lambda *args: errno.ENETUNREACH)
    with pytest.raises(ValueError, match="未得到可判断结果"):
        helper.verify_public("https://site.example", "origin.example")
