import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAIR_TEMPLATE = PROJECT_ROOT / "templates" / "pair_management.html"
COMMUNITY_TEMPLATE = PROJECT_ROOT / "templates" / "community_risk.html"


def _inline_javascript(path):
    html = path.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.DOTALL)
    javascript = "\n".join(scripts)
    return re.sub(r"\{\{.*?\}\}", "null", javascript, flags=re.DOTALL)


@pytest.mark.parametrize("template_path", [PAIR_TEMPLATE, COMMUNITY_TEMPLATE])
def test_changed_template_inline_javascript_is_valid(template_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js 不可用，跳过内嵌脚本语法检查")

    result = subprocess.run(
        [node, "--check", "-"],
        input=_inline_javascript(template_path),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pair_actions_only_confirm_persisted_feedback():
    javascript = _inline_javascript(PAIR_TEMPLATE)

    assert "if (!csrfToken) return false;" in javascript
    assert "if (!response.ok)" in javascript
    assert "return Boolean(result && result.success === true);" in javascript
    assert "const logged = await logEvent('feedback_submitted'" in javascript
    assert "if (logged)" in javascript
    assert "btn.disabled = false;" in javascript
    assert "记录失败，请检查网络后重试。" in javascript


def test_pair_copy_and_countdown_are_failure_safe():
    javascript = _inline_javascript(PAIR_TEMPLATE)
    countdown = javascript[javascript.index("const updateCountdowns"):javascript.index("const csrfTokenEl")]
    copy_handler = javascript[javascript.index("document.querySelectorAll('.copy-reminder')"):javascript.index("document.querySelectorAll('.feedback-btn')")]

    assert "deadline.setDate" not in countdown
    assert "const diff = Math.max(0, deadline - now);" in countdown
    assert "void logEvent('template_copy'" in copy_handler
    assert copy_handler.index("btn.textContent = '已复制';") < copy_handler.index("void logEvent('template_copy'")


def test_community_request_race_and_failure_cleanup_are_guarded():
    javascript = _inline_javascript(COMMUNITY_TEMPLATE)
    load_function = javascript[javascript.index("function loadRiskMap()"):
                               javascript.index("const filterForm")]
    clear_function = javascript[javascript.index("function clearRiskResults(errorCode)"):
                                javascript.index("function renderCharts")]

    assert "riskRequestController.abort();" in load_function
    assert "const requestId = ++riskRequestSequence;" in load_function
    assert "requestOptions.signal = requestController.signal;" in load_function
    assert load_function.count("requestId !== riskRequestSequence") >= 2
    assert "error.name === 'AbortError'" in load_function
    assert load_function.index("error.name === 'AbortError'") < load_function.index("clearRiskResults(error.code)")
    assert load_function.index("const activeLayerKey = document.getElementById('layerSelect').value;") > load_function.index(".then(data =>")

    assert "riskRows = [];" in clear_function
    assert "clearRiskOverlays();" in clear_function
    assert "destroyCharts();" in clear_function
    for element_id in (
        "kpiCommunities",
        "managementSuggestions",
        "impactLikelihoodBody",
        "equityQuartileBody",
        "equityPriorityList",
        "detailTableBody",
        "methodologyList",
    ):
        assert element_id in clear_function
