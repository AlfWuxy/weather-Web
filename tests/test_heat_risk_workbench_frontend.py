"""用 Node 执行真实前端脚本，验证缺失预报、来源标签与跨日行为。"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_heat_risk_workbench_frontend_behaviors():
    node = shutil.which("node")
    if not node:
        pytest.skip("前端行为回归需要 Node.js")
    script = Path(__file__).with_suffix(".js")
    result = subprocess.run(
        [node, "--test", str(script)],
        cwd=script.parent.parent,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
