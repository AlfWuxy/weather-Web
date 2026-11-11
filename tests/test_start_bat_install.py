"""Windows 启动脚本必须用哈希锁定安装依赖。"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_start_bat_pip_install_requires_hashes():
    content = (ROOT / "scripts" / "start.bat").read_text(encoding="utf-8")
    installs = re.findall(r"pip install[^\r\n]*", content)

    assert installs == ["pip install --require-hashes -r requirements.txt"]
    # requirements.txt 没有 Windows wheel，--only-binary=:all: 在本脚本上无法解析。
    assert "not Windows wheels" in content
    assert "--only-binary=:all: cannot resolve" in content
