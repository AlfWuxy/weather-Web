"""实际 SSH 配置解析、临时 TLS 服务和只读命令桩的发布边界回归。"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'scripts/deployment_boundary.py'


def run_helper(*args, data='', env=None):
    return subprocess.run([sys.executable, str(HELPER), *args], input=data, text=True,
                          capture_output=True, env=env, timeout=30)


@pytest.fixture
def ssh_fixture(tmp_path):
    key = tmp_path / 'fixture-key'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    known = tmp_path / 'known_hosts'
    known.write_text('fixture.example ' + key.with_suffix('.pub').read_text())
    known.chmod(0o600)
    config = tmp_path / 'config'
    config.write_text('Host *\n    IdentityAgent none\n')
    config.chmod(0o600)
    control = tmp_path / 'private-control'
    control.mkdir(mode=0o700)
    return known, config, control, key


def ssh_check(fixture, extra='', host='fixture.example'):
    known, config, control, _ = fixture
    options = f'-F {config} -o UserKnownHostsFile={known} {extra}'
    return run_helper('ssh-options', '--target', f'deployer@{host}', '--control-dir', str(control), data=options)


@pytest.mark.parametrize('options', [
    '-o StrictHostKeyChecking=no', '-oStrictHostKeyChecking=off', '-o "StrictHostKeyChecking accept-new"',
    '-o UserKnownHostsFile=/dev/null', '-oUserKnownHostsFile=none', '-o GlobalKnownHostsFile=off',
    '-o KnownHostsCommand=anything', '-o VerifyHostKeyDNS=yes', '-o UpdateHostKeys=yes', '-S /tmp/old-master',
])
def test_ssh_rejects_verification_bypasses_before_connection(ssh_fixture, options):
    assert ssh_check(ssh_fixture, options).returncode == 64


def test_ssh_actual_configuration_requires_matching_preloaded_host(ssh_fixture):
    assert ssh_check(ssh_fixture, host='unknown.example').returncode == 64
    result = ssh_check(ssh_fixture, '-o StrictHostKeyChecking=yes -o ControlMaster=auto -o ControlPersist=300')
    assert result.returncode == 0, result.stderr
    argv = result.stdout.rstrip('\0').split('\0')
    assert argv[:2] == ['-o', 'StrictHostKeyChecking=yes']
    assert f'ControlPath={ssh_fixture[2]}/ssh' in argv
    assert 'ControlPersist=300' in argv


def test_ssh_preserves_port_identity_and_proxyjump(ssh_fixture):
    known, _, _, key = ssh_fixture
    known.write_text('[fixture.example]:2222 ' + key.with_suffix('.pub').read_text())
    result = ssh_check(ssh_fixture, f'-p 2222 -i {key} -J jump.example')
    assert result.returncode == 0, result.stderr
    argv = result.stdout.rstrip('\0').split('\0')
    assert argv[-6:] == ['-p', '2222', '-i', str(key), '-J', 'jump.example']


def test_ssh_rejects_untrusted_known_hosts_permissions(ssh_fixture):
    ssh_fixture[0].chmod(0o666)
    assert ssh_check(ssh_fixture).returncode == 64


@pytest.fixture
def tls_server(tmp_path):
    key, cert = tmp_path / 'tls.key', tmp_path / 'tls.crt'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-keyout', str(key), '-out', str(cert), '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost'], check=True, capture_output=True)
    class Handler(BaseHTTPRequestHandler):
        body = b'{"status":"ok"}'
        mime = 'application/json'
        agents = []
        def do_GET(self):
            self.agents.append(self.headers.get("User-Agent"))
            self.send_response(200)
            self.send_header('Content-Type', self.mime)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(self.body)
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, cert, Handler
    server.shutdown()
    server.server_close()
    thread.join()


def tls_probe(port, cert, host='localhost', trusted=True):
    # 仅测试服务使用临时端口；正式 CLI 的 origin 另行覆盖为固定 HTTPS 443。
    code = 'import deployment_boundary as b,sys; b.public_origin=lambda value:value; b.verify_https(sys.argv[1])'
    env = dict(os.environ, PYTHONPATH=str(HELPER.parent))
    if trusted:
        env['SSL_CERT_FILE'] = str(cert)
    else:
        env.pop('SSL_CERT_FILE', None)
    return subprocess.run([sys.executable, '-c', code, f'https://{host}:{port}'], env=env,
                          text=True, capture_output=True, timeout=10)


def test_real_tls_requires_trusted_certificate_matching_host_and_health_json(tls_server):
    port, cert, handler = tls_server
    assert tls_probe(port, cert).returncode == 0
    assert handler.agents[-1] == 'Mozilla/5.0 (compatible; CaseWeatherDeploy/1.0)'
    assert tls_probe(port, cert, host='127.0.0.1').returncode != 0
    assert tls_probe(port, cert, trusted=False).returncode != 0
    handler.body = b'{"status":"unrelated"}'
    assert tls_probe(port, cert).returncode != 0
    handler.body, handler.mime = b'<html>ok</html>', 'text/html'
    assert tls_probe(port, cert).returncode != 0


@pytest.mark.parametrize('origin', ['http://site.example', 'https://site.example:444', 'https://127.0.0.1',
                                  'https://user@site.example', 'https://site.example/path'])
def test_public_origin_rejects_downgrade_and_ambiguous_targets(origin):
    assert run_helper('public', '--origin', origin).returncode == 64


def origin_probe(tmp_path, *, uid=1001, bind='127.0.0.1', ipv6_fail=False, proxy_public=False):
    commands = tmp_path / 'bin'
    commands.mkdir()
    rows = f'LISTEN 0 128 {bind}:5000 0.0.0.0:* users:(("gunicorn",pid=42,fd=5))\n'
    rows += 'LISTEN 0 128 127.0.0.1:8080 0.0.0.0:* users:(("nginx",pid=43,fd=5))\n'
    if proxy_public:
        rows += 'LISTEN 0 128 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=43,fd=6))\n'
    scripts = {
        'systemctl': 'printf "MainPID=42\\nUser=case-weather\\nActiveState=active\\n"',
        'ss': f'if [ "$1" = -6 ]; then exit {1 if ipv6_fail else 0}; fi\ncat <<\'EOF\'\n{rows}EOF',
        'ip': 'printf \'[{"addr_info":[{"local":"93.184.216.34"}]}]\'',
    }
    for name, body in scripts.items():
        path = commands / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o700)
    code = '''import deployment_boundary as b, json
from types import SimpleNamespace
b.pwd.getpwnam=lambda name: SimpleNamespace(pw_uid=1001)
b.Path.read_text=lambda path: 'Uid: UID UID UID UID\\n'.replace('UID', 'ACTUAL_UID')
print(json.dumps(b.origin_evidence()))
'''.replace('ACTUAL_UID', str(uid))
    env = dict(os.environ, PYTHONPATH=str(HELPER.parent), PATH=f'{commands}:{os.environ["PATH"]}')
    return subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, timeout=10)


def test_origin_actual_command_evidence_accepts_loopback_nonroot(tmp_path):
    result = origin_probe(tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['families'] == [4, 6]


@pytest.mark.parametrize('kwargs', [{'uid': 0}, {'bind': '0.0.0.0'}, {'ipv6_fail': True}, {'proxy_public': True}])
def test_origin_rejects_root_public_listener_and_unknown_ipv6(tmp_path, kwargs):
    assert origin_probe(tmp_path, **kwargs).returncode != 0


def test_external_origin_port_open_and_network_unknown_fail_closed(monkeypatch):
    spec = importlib.util.spec_from_file_location('boundary', HELPER)
    boundary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boundary)
    evidence = {'schema': 1, 'uid': 1001, 'pid': 42, 'loopback_ports': [5000, 8080],
                'families': [4, 6], 'addresses': ['93.184.216.34']}
    class OpenPort:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(boundary.socket, "create_connection", lambda *args, **kwargs: OpenPort())
    with pytest.raises(boundary.BoundaryError):
        boundary.verify_origin_ports(evidence)
    def unknown(*args, **kwargs):
        raise OSError(101, 'Network unreachable')
    monkeypatch.setattr(boundary.socket, "create_connection", unknown)
    with pytest.raises(boundary.BoundaryError):
        boundary.verify_origin_ports(evidence)


def test_deploy_postflight_failure_never_claims_success_or_reenters_transaction():
    content = (ROOT / 'scripts/deploy.sh').read_text()
    tail = content[content.index('# 激活事务已经提交；'):]
    result = subprocess.run(['bash', '-c', 'set -eu; verify_deployment_boundary() { return 1; }; ' + tail],
                            text=True, capture_output=True)
    assert result.returncode == 78
    assert '原子激活已完成' in result.stderr
    assert '未执行二次回滚' in result.stderr
    assert '=== 部署完成 ===' not in result.stdout


def test_deploy_preflight_failure_stops_before_remote_dependency_or_write():
    content = (ROOT / 'scripts/deploy.sh').read_text()
    start = content.index('echo "步骤1: 测试服务器连接..."')
    end = content.index('echo "步骤2.1:', start)
    prefix = 'set -eu; remote_exec() { echo remote-call; }; verify_deployment_boundary() { return 64; }; '
    result = subprocess.run(['bash', '-c', prefix + content[start:end]], text=True, capture_output=True)
    assert result.returncode == 64
    assert result.stdout.count('remote-call') == 1
    assert '步骤2:' not in result.stdout


@pytest.mark.parametrize('status,location,success', [
    (301, 'expected', True), (308, 'expected', True), (200, 'expected', False),
    (403, 'expected', False), (301, 'https://unrelated.example/healthz', False),
    (302, '/healthz', False),
])
def test_http_requires_explicit_same_host_https_redirect(status, location, success):
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(status)
            self.send_header('Location', expected if location == 'expected' else location)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    origin = f'https://localhost:{server.server_port}'
    expected = origin + '/healthz'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code = 'import deployment_boundary as b,sys; b.public_origin=lambda value:value; b.verify_http_redirect(sys.argv[1])'
        result = subprocess.run([sys.executable, '-c', code, origin], text=True, capture_output=True,
                                env=dict(os.environ, PYTHONPATH=str(HELPER.parent)), timeout=10)
        assert (result.returncode == 0) is success, result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
