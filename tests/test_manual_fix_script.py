# -*- coding: utf-8 -*-
"""手动安全修复脚本回归测试。"""

import subprocess
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT_DIR / "scripts" / "complete_manual_fixes.sh"


def _classify_value(env_file, key):
    command = (
        f"source '{SCRIPT_PATH}'; "
        f"value=$(read_env_value '{key}' '{env_file}'); "
        "if is_placeholder_secret \"$value\"; then printf placeholder; else printf configured; fi"
    )
    return subprocess.run(
        ["bash", "-c", command], check=True, capture_output=True, text=True
    ).stdout


@pytest.mark.parametrize("key", ["SECRET_KEY", "PAIR_TOKEN_PEPPER"])
@pytest.mark.parametrize(
    "raw_value",
    [
        "",
        "your-secret-key-here",
        "'change-me-min-32-chars' # 旧示例",
        '\"example-secret\" # 旧示例',
        "YOUR_PLACEHOLDER_VALUE",
    ],
)
def test_manual_fix_script_detects_legacy_placeholders(tmp_path, key, raw_value):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key} = {raw_value}\n", encoding="utf-8")
    assert _classify_value(env_file, key) == "placeholder"


def test_manual_fix_script_accepts_quoted_real_value_with_comment(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'SECRET_KEY="9f50c89b76e94f739918a54e7c47a6218ca55bc7" # production\n',
        encoding="utf-8",
    )
    assert _classify_value(env_file, "SECRET_KEY") == "configured"


def test_manual_fix_script_keeps_generated_env_private():
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert 'chmod 600 "$tmp_file"' in content


def test_manual_fix_script_replaces_and_deduplicates_secret_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SECRET_KEY=first-real-value\n"
        "OTHER_SETTING=kept\n"
        "SECRET_KEY=change-me-min-32-chars\n",
        encoding="utf-8",
    )
    command = (
        f"source '{SCRIPT_PATH}'; "
        f"write_env_value SECRET_KEY generated-value '{env_file}'"
    )

    subprocess.run(["bash", "-c", command], check=True)

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines.count("SECRET_KEY=generated-value") == 1
    assert sum(line.startswith("SECRET_KEY=") for line in lines) == 1
    assert "OTHER_SETTING=kept" in lines
    assert env_file.stat().st_mode & 0o777 == 0o600
