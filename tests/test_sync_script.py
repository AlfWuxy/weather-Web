# -*- coding: utf-8 -*-
"""旧同步入口必须复用不可变发布流程。"""

from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT_DIR / "scripts" / "sync.sh"


def test_sync_script_delegates_to_immutable_deploy():
    content = SYNC_SCRIPT.read_text(encoding="utf-8")

    assert 'set -euo pipefail' in content
    assert 'exec "$SCRIPT_DIR/deploy.sh" "$@"' in content
    assert 'rsync ' not in content
    assert 'systemctl restart' not in content
    assert 'DEPLOY_PROJECT_DIR' not in content
