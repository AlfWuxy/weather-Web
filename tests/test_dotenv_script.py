# -*- coding: utf-8 -*-
"""部署 dotenv 解析器回归测试。"""

import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dotenv.sh"


def _normalize(value):
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; normalize_env_value "$2"; printf "%s" "$NORMALIZED_ENV_VALUE"',
            "dotenv-test",
            str(SCRIPT),
            value,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_quoted_hash_and_spaces_are_preserved():
    assert _normalize('"a#b c" # trailing comment') == "a#b c"
    assert _normalize("'a#b c'") == "a#b c"


def test_unquoted_comment_is_removed_only_after_whitespace():
    assert _normalize("value#kept") == "value#kept"
    assert _normalize("value # removed") == "value"
