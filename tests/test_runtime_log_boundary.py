# -*- coding: utf-8 -*-
"""正式运行时日志边界测试。"""

import subprocess

import pytest

from scripts.verify_runtime_log_boundary import (
    LogBoundaryError,
    verify_active_nginx,
    verify_nginx_site_access_log,
)


def _write_config(tmp_path, body):
    path = tmp_path / "case-weather"
    path.write_text(body, encoding="utf-8")
    return path


def test_target_site_requires_single_explicit_access_log_off(tmp_path):
    path = _write_config(
        tmp_path,
        """
server {
    listen 127.0.0.1:80;
    server_name yilaoweather.org www.yilaoweather.org;
    access_log off;
    error_log /dev/null crit;
    location / { proxy_pass http://127.0.0.1:5000; }
}
""",
    )
    verify_nginx_site_access_log(path)


@pytest.mark.parametrize(
    "directives",
    [
        "error_log /dev/null crit;",
        "access_log /var/log/nginx/access.log; error_log /dev/null crit;",
        "access_log off; error_log /var/log/nginx/error.log;",
        "access_log off; error_log /dev/null error;",
        "access_log off; error_log /dev/null crit; access_log off;",
        "access_log off; error_log /dev/null crit; error_log /dev/null crit;",
        "location /private { access_log off; error_log /dev/null crit; }",
    ],
)
def test_target_site_rejects_missing_enabled_or_ambiguous_log_boundary(tmp_path, directives):
    path = _write_config(
        tmp_path,
        f"server {{ server_name yilaoweather.org; {directives} }}",
    )
    with pytest.raises(LogBoundaryError):
        verify_nginx_site_access_log(path)


def test_other_site_access_log_does_not_satisfy_target_boundary(tmp_path):
    path = _write_config(
        tmp_path,
        """
server { server_name example.org; access_log off; }
server { server_name yilaoweather.org; }
""",
    )
    with pytest.raises(LogBoundaryError):
        verify_nginx_site_access_log(path)


def test_nested_or_commented_directives_cannot_satisfy_boundary(tmp_path):
    path = _write_config(
        tmp_path,
        """
# server { server_name yilaoweather.org; access_log off; error_log /dev/null crit; }
server {
    server_name yilaoweather.org;
    location / {
        access_log off;
        error_log /dev/null crit;
    }
}
""",
    )
    with pytest.raises(LogBoundaryError):
        verify_nginx_site_access_log(path)


def test_target_name_inside_nested_block_does_not_match(tmp_path):
    path = _write_config(
        tmp_path,
        """
server {
    server_name example.org;
    access_log off;
    error_log /dev/null crit;
    location / { set $upstream_name "yilaoweather.org"; }
}
""",
    )
    with pytest.raises(LogBoundaryError):
        verify_nginx_site_access_log(path)


@pytest.mark.parametrize(
    "nested",
    [
        "location /private { access_log /tmp/private.log; }",
        "location /private { error_log /tmp/private-error.log; }",
        "location /private { access_log off; }",
        "include /etc/nginx/snippets/private.conf;",
        "location /private { include /etc/nginx/snippets/private.conf; }",
    ],
)
def test_target_rejects_nested_log_overrides_and_includes(tmp_path, nested):
    path = _write_config(
        tmp_path,
        f"""
server {{
    server_name yilaoweather.org;
    access_log off;
    error_log /dev/null crit;
    {nested}
}}
""",
    )
    with pytest.raises(LogBoundaryError):
        verify_nginx_site_access_log(path)


@pytest.mark.parametrize(
    "server_name",
    ["evil-yilaoweather.org", "yilaoweather.org.evil.example", "example.org"],
)
def test_target_domain_requires_exact_token(tmp_path, server_name):
    path = _write_config(
        tmp_path,
        f"server {{ server_name {server_name}; access_log off; error_log /dev/null crit; }}",
    )
    with pytest.raises(LogBoundaryError):
        verify_nginx_site_access_log(path)


def test_active_nginx_uses_fixed_binary_and_never_echoes_config(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "server { server_name yilaoweather.org; "
                "access_log off; error_log /dev/null crit; }"
            ),
            stderr="nginx: configuration test is successful",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    verify_active_nginx()

    assert calls[0][0] == ["/usr/sbin/nginx", "-T"]
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 15


def test_active_nginx_rejects_failed_syntax_without_echoing_output(monkeypatch):
    sentinel = "CONFIG_SECRET_SENTINEL"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=sentinel, stderr=sentinel
        ),
    )
    with pytest.raises(LogBoundaryError) as captured:
        verify_active_nginx()
    assert sentinel not in str(captured.value)
