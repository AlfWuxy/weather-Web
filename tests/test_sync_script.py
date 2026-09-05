# -*- coding: utf-8 -*-
"""快速同步脚本的失败传播回归测试。"""

import os
import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT_DIR / "scripts" / "sync.sh"


def test_sync_script_stops_when_upload_fails(tmp_path):
    """上传失败后必须返回失败，不能继续重启或打印完成。"""
    fake_rsync = tmp_path / "rsync"
    fake_rsync.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    fake_rsync.chmod(0o755)

    ssh_marker = tmp_path / "ssh-called"
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        f"#!/bin/sh\ntouch '{ssh_marker}'\nexit 0\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "DEPLOY_SERVER": "example.invalid",
        "DEPLOY_USER": "test-user",
        "DEPLOY_LOCAL_DIR": str(ROOT_DIR),
        "ENV_FILE": str(tmp_path / "missing.env"),
        "PATH": f"{tmp_path}{os.pathsep}{env['PATH']}",
    })
    for key in ("DEPLOY_PASSWORD", "SSHPASS"):
        env.pop(key, None)

    result = subprocess.run(
        ["bash", str(SYNC_SCRIPT)],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23
    assert "同步完成" not in result.stdout
    assert not ssh_marker.exists()


def test_sync_script_expect_branch_propagates_child_status():
    """密码交互分支也必须返回 rsync/ssh 的真实退出码。"""
    content = SYNC_SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in content
    assert content.count("set wait_result [wait]") == 2
    assert content.count("exit [lindex \\$wait_result 3]") == 2


def test_sync_script_excludes_root_analysis_dir():
    content = SYNC_SCRIPT.read_text(encoding="utf-8")
    flag = "--exclude=/analysis/"

    assert content.count(flag) == 3
    assert "--exclude '/analysis/'" not in content

    rsync_starts = [idx for idx in range(len(content)) if content.startswith("rsync -avz", idx)]
    assert len(rsync_starts) == 3
    for start in rsync_starts:
        end = content.find("$PROJECT_DIR/", start)
        assert end != -1
        assert flag in content[start:end]
