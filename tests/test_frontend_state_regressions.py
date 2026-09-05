import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAIR_TEMPLATE = PROJECT_ROOT / "templates" / "pair_management.html"
ACTION_TEMPLATE = PROJECT_ROOT / "templates" / "action_checkin.html"
COMMUNITY_TEMPLATE = PROJECT_ROOT / "templates" / "community_risk.html"


def _inline_javascript(path):
    html = path.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.DOTALL)
    javascript = "\n".join(scripts)
    javascript = re.sub(r"\{\{.*?\}\}", "null", javascript, flags=re.DOTALL)
    return re.sub(r"\{%.*?%\}", "", javascript, flags=re.DOTALL)


def _run_storage_recovery(template_path, storage_key, stored_value, *, contact_fields=False):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js 不可用，跳过内嵌脚本运行检查")

    elements = """
        const emptyClassList = { add() {}, remove() {} };
        const elements = {
            localContactName: { value: '' },
            localContactPhone: { value: '' },
            saveLocalContact: {
                addEventListener() {},
                classList: emptyClassList,
                textContent: ''
            }
        };
    """ if contact_fields else "const elements = {};"
    harness = f"""
        let domReady = null;
        const removedKeys = [];
        const values = new Map([
            [{json.dumps(storage_key)}, {json.dumps(stored_value)}],
            ['unrelated-key', 'keep-me']
        ]);
        const localStorage = {{
            getItem(key) {{ return values.has(key) ? values.get(key) : null; }},
            setItem(key, value) {{ values.set(key, String(value)); }},
            removeItem(key) {{ removedKeys.push(key); values.delete(key); }}
        }};
        {elements}
        const document = {{
            addEventListener(event, callback) {{
                if (event === 'DOMContentLoaded') domReady = callback;
            }},
            getElementById(id) {{ return elements[id] || null; }},
            querySelectorAll() {{ return []; }},
            querySelector() {{ return null; }},
            createElement() {{ return {{}}; }}
        }};
        const window = {{}};
        const setInterval = () => 0;
        const setTimeout = () => 0;
        const alert = () => {{}};
        {_inline_javascript(template_path)}
        if (typeof domReady !== 'function') throw new Error('missing DOMContentLoaded');
        domReady();
        if (!removedKeys.includes({json.dumps(storage_key)})) {{
            throw new Error('invalid storage key was not removed');
        }}
        if (values.get('unrelated-key') !== 'keep-me') {{
            throw new Error('unrelated storage was changed');
        }}
    """
    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "template_path",
    [PAIR_TEMPLATE, ACTION_TEMPLATE, COMMUNITY_TEMPLATE],
)
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


def test_screening_exclusion_creates_gray_amap_overlay_at_runtime():
    """partial 筛查中有坐标的排除行应真正绘制灰色点并显示原因。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js 不可用，跳过 AMap 运行时检查")

    javascript = _inline_javascript(COMMUNITY_TEMPLATE).replace(
        "const communityCoords = null;",
        "const communityCoords = {'证据不足村':[116.2,29.27]};",
    )
    harness = """
        const addedOverlays = [];
        class MapStub {
            constructor() { this.handlers = {}; }
            add(items) { addedOverlays.push(items); }
            remove() {}
            addControl() {}
            on(event, callback) { this.handlers[event] = callback; }
            setFitView() {}
            setZoomAndCenter() {}
        }
        class OverlayStub {
            constructor(options) { this.options = options; this.handlers = {}; }
            on(event, callback) { this.handlers[event] = callback; }
        }
        class InfoWindowStub extends OverlayStub { open() {} }
        const AMap = {
            Map: MapStub,
            Circle: OverlayStub,
            Marker: OverlayStub,
            InfoWindow: InfoWindowStub,
            Pixel: class { constructor(x, y) { this.x = x; this.y = y; } },
            Scale: class {},
            ToolBar: class {},
            plugin(_names, callback) { callback(); }
        };
        const window = {};
        const document = {
            readyState: 'loading',
            addEventListener() {},
            getElementById() { return null; },
            querySelectorAll() { return []; },
            querySelector() { return null; },
            createElement() { return {}; },
            createTextNode(value) { return value; }
        };
        const setInterval = () => 0;
        const setTimeout = () => 0;
        const alert = () => {};
    """ + javascript + """
        activeRankingMode = SCREENING_RANKING_MODE;
        mapReady = true;
        addRiskOverlays([{
            community: '证据不足村',
            ranking_eligible: false,
            screening_score: null,
            screening_color: '#94a3b8',
            data_message: 'Q3 覆盖证据不足'
        }], 'screening_score');

        if (addedOverlays.length !== 1 || addedOverlays[0].length !== 2) {
            throw new Error('未生成排除社区的 Circle 和 Marker');
        }
        const [circle, marker] = addedOverlays[0];
        if (circle.options.strokeColor !== '#94a3b8' || circle.options.fillColor !== '#94a3b8') {
            throw new Error('排除社区地图圆不是灰色');
        }
        if (!marker.options.content.includes('background:#94a3b8')) {
            throw new Error('排除社区地图标记不是灰色');
        }
        if (riskOverlays.length !== 1
                || !riskOverlays[0].infoWindow.options.content.includes('Q3 覆盖证据不足')) {
            throw new Error('排除原因未进入地图弹窗');
        }
    """

    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pair_contacts_recover_from_invalid_array_items_at_runtime():
    _run_storage_recovery(
        PAIR_TEMPLATE,
        'heat_action_alt_contacts',
        '[null]',
    )


def test_action_contact_recovers_from_invalid_field_types_at_runtime():
    _run_storage_recovery(
        ACTION_TEMPLATE,
        'heat_action_contact',
        '{"name":{"unexpected":true},"phone":"123"}',
        contact_fields=True,
    )


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
