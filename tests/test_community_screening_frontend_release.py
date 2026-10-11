import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMUNITY_TEMPLATE = PROJECT_ROOT / "templates" / "community_risk.html"


def _inline_javascript(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.DOTALL)
    javascript = "\n".join(scripts)
    javascript = re.sub(r"\{\{.*?\}\}", "null", javascript, flags=re.DOTALL)
    return re.sub(r"\{%.*?%\}", "", javascript, flags=re.DOTALL)


def _source_between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_pending_shell_hides_both_tracks_and_has_no_clinical_leak(admin_client):
    response = admin_client.get("/community-risk")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    page = re.search(r'<div\b[^>]*id="communityRiskPage"[^>]*>', html)
    assert page
    assert 'data-ranking-mode="pending"' in page.group(0)
    assert 'aria-busy="true"' in page.group(0)

    mode_surfaces = re.findall(
        r'<(?:div|span)\b[^>]*data-(?:clinical|screening)-only[^>]*>',
        html,
    )
    assert mode_surfaces
    assert all("hidden" in surface and "d-none" in surface for surface in mode_surfaces)

    profile_alert = re.search(
        r'<div\b[^>]*id="profileDataAlert"[^>]*>(.*?)</div>',
        html,
        flags=re.DOTALL,
    )
    methodology = re.search(
        r'<ul\b[^>]*id="methodologyList"[^>]*>(.*?)</ul>',
        html,
        flags=re.DOTALL,
    )
    assert profile_alert and methodology
    initial_copy = profile_alert.group(1) + methodology.group(1)
    assert "正在确认当前可用的社区数据和分析轨道" in initial_copy
    assert "正在载入当前模式的方法与证据边界" in initial_copy
    assert "数据不足，未参与排名" not in initial_copy
    assert "完整性门" not in initial_copy
    assert "BaselineVisits" not in initial_copy

    # 正式完整画像仍存在，只在确认正式模式后显示。
    assert "Impact × Likelihood" in html
    assert "公平性分层（脆弱社区优先）" in html
    assert 'id="detailTableBody"' in html
    assert 'id="screeningDetailTableBody"' in html
    refresh_button = re.search(r'<button\b[^>]*id="refreshRiskMap"[^>]*>', html)
    assert refresh_button
    assert "no-loading" in refresh_button.group(0)


def test_screening_mode_contract_and_amap_independent_startup(admin_client):
    html = admin_client.get("/community-risk").get_data(as_text=True)
    javascript = "\n".join(
        re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.DOTALL)
    )

    map_complete = _source_between(
        javascript,
        "if (map) {\n    map.on('complete'",
        "let communityRiskInitialized",
    )
    initializer = _source_between(
        javascript,
        "function initializeCommunityRiskPage()",
        "if (document.readyState === 'loading')",
    )
    methodology_filter = _source_between(
        javascript,
        "function screeningMethodologyFromMetadata",
        "function median",
    )
    screening_table = _source_between(
        javascript,
        "function renderScreeningDetailTable",
        "function renderMethodology",
    )

    assert "mapReady = true" in map_complete
    assert "redrawCurrentRiskMap()" in map_complete
    assert "loadRiskMap()" not in map_complete
    assert "applyPendingMode()" in initializer
    assert "loadRiskMap()" in initializer
    assert "DOMContentLoaded" in javascript

    assert "fullProfileFragments" in methodology_filter
    assert "数据不足，未参与排名" in methodology_filter
    assert "ASPECT 65+" in html
    assert "夏季晴空地表温度" in html
    assert "树木覆盖类面积占比" in html
    assert "q3_lst_c_mean" in screening_table
    assert "tree_cover_pct" in screening_table
    assert "q3_coverage_pct" in screening_table
    assert "screeningRankLabel" in screening_table
    assert "DEFAULT_SCREENING_BANDS" in javascript
    assert "function screeningBandsFromMetadata" in javascript
    assert "details.screening_bands" in javascript
    assert "screeningBandsFromMetadata().find" in javascript
    assert "screeningBandLegendText" in javascript
    assert "max_inclusive" in javascript
    assert "Q4：相对脆弱性较高" not in html


def test_screening_metrics_have_visible_explanations_and_metric_details(admin_client):
    html = admin_client.get("/community-risk").get_data(as_text=True)

    assert '<details class="card cr-card mb-3 d-none" data-screening-only hidden>' in html
    assert '结果依据：指标、公式与空间尺度</summary>' in html
    assert '<h4 class="label">热脆弱性对比分' in html
    assert '<h4 class="label">有效观测覆盖' in html
    assert "三个主题在当前资料完整社区中分别转成相对分位" in html
    assert "ASPECT 基于 2020 年人口资料估计各网格的年龄分布" in html
    assert "Aqua MODIS 白天晴空地表温度" in html
    assert "使用 100−树木覆盖类面积占比" in html
    assert "覆盖率用于判断数据质量，不计入对比分" in html
    assert "同一网格内的社区使用相同的三个原始指标，对比分相同、名次并列" in html
    # 用户不展开方法也能看到年份、比较范围、数据质量与缺值状态。
    assert "历史数据 · 县内相对比较" in html
    assert "人口：ASPECT 2020 年估计；树木覆盖类面积占比：ESA 2020 年" in html
    assert "有效观测覆盖表示数据质量；灰色为资料不足" in html
    assert "本模式不提供预计额外就诊、SIR" not in html

    for metric_key in (
        "community_screening_score",
        "gis_native_grid",
        "gis_age65_share",
        "gis_lst_mean",
        "gis_tree_cover",
        "gis_q3_coverage",
    ):
        assert f'data-metric-info="{metric_key}"' in html

    assert '/transparency#community-screening-score' in html
    assert 'aria-label="查看“社区热脆弱性对比”的计算说明"' in html


def test_successful_refresh_restores_mode_specific_button_state():
    """成功请求结束后必须恢复刷新按钮，不能停在“正在刷新”。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js 不可用，跳过刷新按钮运行时检查")

    javascript = _inline_javascript(COMMUNITY_TEMPLATE)
    restore_controls = _source_between(
        javascript,
        "function restoreRiskRequestControls",
        "function loadRiskMap",
    )
    load_risk_map = _source_between(
        javascript,
        "function loadRiskMap",
        "const filterForm",
    )
    harness = """
        const SCREENING_RANKING_MODE = 'exploratory_geospatial_screening';
        let activeRankingMode = SCREENING_RANKING_MODE;
        function isScreeningMode(mode = activeRankingMode) {
            return mode === SCREENING_RANKING_MODE;
        }
    """ + restore_controls + """
        const page = {
            busy: 'true',
            setAttribute(_name, value) { this.busy = value; }
        };
        const button = { disabled: true };
        const label = { textContent: '正在刷新...' };

        restoreRiskRequestControls(page, button, label);
        if (page.busy !== 'false' || button.disabled || label.textContent !== '刷新社区对比') {
            throw new Error('社区对比刷新控件未恢复');
        }

        activeRankingMode = 'full_profile';
        page.busy = 'true';
        button.disabled = true;
        label.textContent = '正在刷新...';
        restoreRiskRequestControls(page, button, label);
        if (page.busy !== 'false' || button.disabled || label.textContent !== '查看社区风险') {
            throw new Error('正式风险刷新控件未恢复');
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
    assert "let requestRendered = false" in load_risk_map
    assert "requestRendered = true" in load_risk_map
    assert "if (requestRendered)" in load_risk_map
    assert "restoreRiskRequestControls(page, refreshButton, refreshLabel)" in load_risk_map


def test_partial_screening_exclusion_creates_gray_amap_overlay_at_runtime():
    """partial 筛查中有坐标的排除行要绘制灰点并展示可审计原因。"""
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
