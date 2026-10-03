"""同步入口共用安全传输边界；进程失败测试见test_deployment_security。"""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]


def test_sync_requires_existing_boundary_before_upload():
    content = (ROOT / "scripts/sync.sh").read_text()
    assert content.index("verify_deployment_boundary") < content.index("upload_files")
    assert content.count("verify_deployment_boundary") == 2
    assert "source \"$SCRIPT_DIR/deployment_security.sh\"" in content


def test_shared_transport_keeps_failures_and_private_files_excluded():
    content = (ROOT / "scripts/deployment_security.sh").read_text()
    assert "set -euo pipefail" in content
    assert "StrictHostKeyChecking=yes" in content
    assert "--exclude=/analysis/" in content
    assert "--exclude '.env*'" in content
    assert "send \"yes" not in content
