# -*- coding: utf-8 -*-
import math
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from core.db_models import Community, MedicalRecord
from core.time_utils import utcnow


class _CommunityRiskModeParser(HTMLParser):
    """收集首屏模式节点，避免测试依赖第三方 HTML 解析器。"""

    def __init__(self):
        super().__init__()
        self.mode_surfaces = []
        self.clinical_layer_options = []
        self.elements_by_id = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get('class') or '').split())
        record = {
            'tag': tag,
            'attributes': attributes,
            'classes': classes,
        }
        if 'data-clinical-only' in attributes or 'data-screening-only' in attributes:
            self.mode_surfaces.append(record)
        if 'data-clinical-layer' in attributes:
            self.clinical_layer_options.append(record)
        if attributes.get('id'):
            self.elements_by_id[attributes['id']] = record


def _inline_javascript(html):
    """拼接页面内联脚本，供前端行为契约测试定位。"""
    return '\n'.join(re.findall(r'<script\b[^>]*>(.*?)</script>', html, flags=re.DOTALL))


def _source_between(source, start_marker, end_marker):
    """提取两个稳定标记之间的源码并给出明确失败信息。"""
    start = source.find(start_marker)
    assert start >= 0, f'未找到前端契约起点：{start_marker}'
    end = source.find(end_marker, start + len(start_marker))
    assert end >= 0, f'未找到前端契约终点：{end_marker}'
    return source[start:end]


def _fresh_qweather(**overrides):
    """生成满足社区风险生产门的和风实况夹具。"""
    payload = {
        'temperature': 30.0,
        'temperature_max': 34.0,
        'temperature_min': 25.0,
        'humidity': 65.0,
        'pressure': 1005.0,
        'wind_speed': 1.8,
        'weather_condition': '晴',
        'aqi': 45.0,
        'pm25': 20.0,
        'air_quality_available': True,
        'observed_at': utcnow().isoformat(),
        'air_observed_at': utcnow().isoformat(),
        'quality_version': 1,
        'data_source': 'QWeather',
        'is_mock': False,
    }
    payload.update(overrides)
    return payload


def _seed_community_risk_data(db_session):
    communities = [
        Community(name='甲村', population=1200, elderly_ratio=0.33, chronic_disease_ratio=0.12),
        Community(name='乙村', population=680, elderly_ratio=0.41, chronic_disease_ratio=0.17),
        Community(name='丙村', population=540, elderly_ratio=0.52, chronic_disease_ratio=0.21),
    ]
    db_session.add_all(communities)

    start_day = datetime(2025, 10, 1, 8, 0, tzinfo=timezone.utc)
    for i in range(30):
        day = start_day + timedelta(days=i)
        records = {
            '甲村': 1 if i % 3 != 0 else 0,
            '乙村': 2 if i % 2 == 0 else 1,
            '丙村': 3 if i % 4 == 0 else 1,
        }
        for community, visits in records.items():
            for visit_idx in range(visits):
                db_session.add(MedicalRecord(
                    patient_name=f'{community}-样本-{i}-{visit_idx}',
                    gender='男' if visit_idx % 2 == 0 else '女',
                    age=68 if community == '丙村' else 52,
                    visit_time=day,
                    disease_category='呼吸系统',
                    community=community
                ))

    db_session.commit()


def _complete_profile(name, population, elderly_ratio, chronic_ratio, *, db_coords=None):
    """生成字段完整的已核验社区测试档案。"""
    db_longitude, db_latitude = db_coords or (10.0, 10.0)
    return {
        'id': name,
        'name': name,
        'location': f'{name}测试地址',
        'longitude': db_longitude,
        'latitude': db_latitude,
        'population': population,
        'elderly_ratio': elderly_ratio,
        'chronic_disease_ratio': chronic_ratio,
        'green_space_ratio': 0.22,
        'heat_island_index': 0.48,
        'medical_accessibility': 0.72,
        'baseline_visits': max(population * 0.01, 1.0),
        'uses_proxy_values': False,
    }


def _install_complete_service(authenticated_client, monkeypatch, profiles, coords_map):
    """安装完整档案服务，同时保留真实测试病例查询。"""
    import services.community_risk_service as risk_module
    from services.community_risk_cache import clear_local_community_risk_cache

    service = risk_module.CommunityRiskService()
    service.community_profiles = profiles
    service.community_profile_status = {
        'available': True,
        'code': 'available',
        'source': 'verified_test_profiles',
        'message': '测试档案字段完整。',
    }
    # 请求期间保留测试档案，避免 ORM 缺失的字段覆盖正向夹具。
    monkeypatch.setattr(service, '_load_community_profiles', lambda: None)
    monkeypatch.setattr(risk_module, '_community_service', service)
    monkeypatch.setitem(
        authenticated_client.application.config,
        'COMMUNITY_COORDS_GCJ',
        coords_map,
    )
    clear_local_community_risk_cache()
    return service


def test_community_risk_page_has_academic_sections(authenticated_client):
    response = authenticated_client.get('/community-risk')
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert '社区空间分析' in html
    assert '正在确认可用数据与分析模式' in html
    # 正式完整画像轨道仍保留，由 API 成功后的模式切换显示。
    assert '社区风险与行动地图' in html
    assert '查看哪些社区需要优先提醒、走访和安排避暑资源' in html
    assert '地图显示' in html
    assert '天气与预警' in html
    assert '社区脆弱性' in html
    assert '历史健康负担' in html
    assert 'Impact × Likelihood' in html
    assert '公平性分层（脆弱社区优先）' in html
    assert 'id="layerSelect"' in html
    assert '社区风险明细' in html
    assert '项目综合风险等级 0-4' in html
    visible_html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    assert 'HeatRisk' not in visible_html
    assert 'data-metric-info="community_risk_index"' in html
    assert "element.classList.toggle('d-none', !visible)" in html
    assert '人工分流、核查与行动排序' in html
    assert '自动决策' not in html
    assert 'probability_exceed_baseline || 0' not in html
    assert '查看计算说明' in html
    assert 'width:min(300px,calc(100vw - 72px))' in html
    assert 'min-width:300px' not in html
    assert '优先安排提醒和走访' in html
    assert '社区排序将在天气更新后显示' in html
    assert '加载失败：' not in html
    assert 'BaselineVisits' in html
    assert 'id="profileDataAlert"' in html
    assert '数据不足，未参与排名' in html
    assert '不生成预计就诊或行动优先级' in html
    assert 'config.COMMUNITY_COORDS_GCJ' in html
    assert '稳定的中性代理值' not in html
    assert 'const rowLng = toNumber' not in html
    assert 'communities.find' not in html


def test_community_risk_initial_dom_is_neutral_and_fail_closed(authenticated_client):
    response = authenticated_client.get('/community-risk')
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    parser = _CommunityRiskModeParser()
    parser.feed(html)
    page = parser.elements_by_id['communityRiskPage']
    assert page['attributes']['data-ranking-mode'] == 'pending'
    assert page['attributes']['aria-busy'] == 'true'
    assert parser.mode_surfaces
    for surface in parser.mode_surfaces:
        assert 'hidden' in surface['attributes']
        assert 'd-none' in surface['classes']

    assert parser.clinical_layer_options
    for option in parser.clinical_layer_options:
        assert 'hidden' in option['attributes']
        assert 'disabled' in option['attributes']

    refresh = parser.elements_by_id['refreshRiskMap']
    assert 'disabled' in refresh['attributes']

    profile_alert = re.search(
        r'<div\b[^>]*id="profileDataAlert"[^>]*>(.*?)</div>',
        html,
        flags=re.DOTALL,
    )
    methodology_list = re.search(
        r'<ul\b[^>]*id="methodologyList"[^>]*>(.*?)</ul>',
        html,
        flags=re.DOTALL,
    )
    assert profile_alert
    assert methodology_list
    initial_copy = profile_alert.group(1) + methodology_list.group(1)
    assert '正在确认当前可用的社区数据和分析轨道' in initial_copy
    assert '正在载入当前模式的方法与证据边界' in initial_copy
    assert '数据不足，未参与排名' not in initial_copy
    assert '完整性门' not in initial_copy
    assert 'BaselineVisits' not in initial_copy


def test_screening_methodology_only_accepts_screening_metadata(authenticated_client):
    html = authenticated_client.get('/community-risk').get_data(as_text=True)
    javascript = _inline_javascript(html)

    metadata_resolver = _source_between(
        javascript,
        'function resolveScreeningMetadata',
        'function screeningMethodologyFromMetadata',
    )
    methodology_filter = _source_between(
        javascript,
        'function screeningMethodologyFromMetadata',
        'function median',
    )
    load_function = _source_between(
        javascript,
        'function loadRiskMap()',
        'const filterForm',
    )
    screening_success = _source_between(
        load_function,
        'if (isScreeningMode()) {\n            renderScreeningDetailTable',
        '} else {',
    )

    assert metadata_resolver.count('ranking_mode === SCREENING_RANKING_MODE') == 2
    assert 'data && data.ranking_metadata' in metadata_resolver
    assert 'rankingPayload && rankingPayload.metadata' in metadata_resolver
    assert 'metadata.ranking_mode !== SCREENING_RANKING_MODE' in methodology_filter
    assert 'Array.isArray(metadata.methodology)' in methodology_filter
    assert 'fullProfileFragments' in methodology_filter
    assert '数据不足，未参与排名' in methodology_filter
    assert 'screeningMethodologyFromMetadata(activeRankingMetadata)' in screening_success
    assert 'data.methodology' not in screening_success
    assert 'rankingPayload.methodology' not in load_function
    # 正式完整画像仍使用原有顶层方法说明。
    assert 'renderMethodology(data.methodology || [])' in load_function


def test_risk_data_request_is_independent_from_amap_complete(authenticated_client):
    html = authenticated_client.get('/community-risk').get_data(as_text=True)
    javascript = _inline_javascript(html)

    map_complete = _source_between(
        javascript,
        "if (map) {\n    map.on('complete'",
        'let communityRiskInitialized',
    )
    initializer = _source_between(
        javascript,
        'function initializeCommunityRiskPage()',
        "if (document.readyState === 'loading')",
    )
    startup = javascript[javascript.index("if (document.readyState === 'loading')"):]
    redraw = _source_between(
        javascript,
        'function redrawCurrentRiskMap()',
        'function renderCharts',
    )
    load_function = _source_between(
        javascript,
        'function loadRiskMap()',
        'const filterForm',
    )
    success_path = _source_between(load_function, '.then(data => {', '.catch(error => {')

    assert 'mapReady = true' in map_complete
    assert 'redrawCurrentRiskMap()' in map_complete
    assert 'loadRiskMap()' not in map_complete
    assert 'applyPendingMode()' in initializer
    assert 'loadRiskMap()' in initializer
    assert "document.addEventListener('DOMContentLoaded', initializeCommunityRiskPage" in startup
    assert '} else {\n    initializeCommunityRiskPage();' in startup
    assert 'layerSelectElement.value' in redraw
    assert 'if (mapReady && riskRows && riskRows.length > 0)' in redraw
    assert 'addRiskOverlays(riskRows, activeLayerKey)' in redraw
    assert success_path.index('riskRows =') < success_path.index('redrawCurrentRiskMap()')
    # API 已成功时直接按 ranking_mode 渲染，天气缺失只属于请求失败分支。
    assert 'applyRankingMode(rankingMode)' in success_path
    assert 'weather_unavailable' not in success_path


def test_screening_rows_use_api_color_and_exclusions_never_receive_rank(authenticated_client):
    html = authenticated_client.get('/community-risk').get_data(as_text=True)
    javascript = _inline_javascript(html)

    score_function = _source_between(
        javascript,
        'function screeningScore',
        'function screeningExclusionReason',
    )
    eligibility_function = _source_between(
        javascript,
        'function rowIsRanked',
        'function rowHasMapCoordinate',
    )
    style_function = _source_between(
        javascript,
        'function getLayerStyle',
        'function renderLayerLegend',
    )
    ranking_function = _source_between(
        javascript,
        'function renderRanking',
        'function renderSuggestions',
    )
    screening_table = _source_between(
        javascript,
        'function renderScreeningDetailTable',
        'function renderMethodology',
    )
    map_overlays = _source_between(
        javascript,
        'function addRiskOverlays',
        'function focusOnCommunity',
    )

    assert 'row && row.screening_score' in score_function
    assert 'risk_index' not in score_function
    assert 'row && row.ranking_eligible === false' in eligibility_function
    assert 'if (!rowIsRanked(row))' in style_function
    assert "color: '#94a3b8', label: '未进入筛查'" in style_function
    assert 'screeningColor(row, fallbackColor)' in style_function
    assert 'row && row.screening_color' in javascript
    assert "? (rankingEligible ? screeningRankLabel(row, index) : '未进入筛查')" in ranking_function
    assert 'screeningExclusionReason(row)' in ranking_function
    assert "rankingEligible ? screeningRankLabel(row, index) : '未进入筛查'" in screening_table
    assert "rankingEligible ? '已进入筛查' : screeningExclusionReason(row)" in screening_table
    assert 'row && row.ranking_eligible === false' in map_overlays
    assert 'if (!shouldDraw || !rowHasMapCoordinate(row))' in map_overlays


def test_screening_status_explains_evidence_failure_and_weather_context(authenticated_client):
    html = authenticated_client.get('/community-risk').get_data(as_text=True)
    javascript = _inline_javascript(html)
    profile_status = _source_between(
        javascript,
        'function renderProfileDataStatus',
        'function renderSummary',
    )

    assert "activeRankingStatus === 'unavailable'" in profile_status
    assert 'activeRankingMetadata.data_message' in profile_status
    assert 'activeRankingMetadata.message' in profile_status
    assert 'activeRankingMetadata.reason' in profile_status
    assert 'summary.data_message' in profile_status
    assert '探索性筛查证据不可用：${unavailableReason}' in profile_status
    assert 'activeRankingMetadata.weather_context_available === false' in profile_status
    assert '实时天气上下文不可用，但未参与本筛查，排名仍可使用。' in profile_status


def test_community_risk_api_returns_extended_fields(
    authenticated_client,
    db_session,
    monkeypatch,
):
    _seed_community_risk_data(db_session)
    profiles = {
        '甲村': _complete_profile('甲村', 1200, 0.33, 0.12, db_coords=(10.0, 10.0)),
        '乙村': _complete_profile('乙村', 680, 0.41, 0.17, db_coords=(20.0, 20.0)),
        '丙村': _complete_profile('丙村', 540, 0.52, 0.21, db_coords=(30.0, 30.0)),
    }
    configured_coords = {
        '甲村': [116.201, 29.331],
        '乙村': [116.202, 29.332],
        '丙村': [116.203, 29.333],
    }
    _install_complete_service(
        authenticated_client,
        monkeypatch,
        profiles,
        configured_coords,
    )

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={
            'analysis_date': '2025-10-30',
            'window_days': 30,
            'disease': '呼吸系统',
            'weather': _fresh_qweather()
        },
        headers={'X-CSRF-Token': 'test-csrf-token'}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True

    assert 'rankings' in payload
    assert len(payload['rankings']) >= 1
    first = payload['rankings'][0]
    assert first['ranking_eligible'] is True
    assert first['data_status'] == 'available'
    assert first['coordinate_available'] is True
    assert first['coordinate_source'] == 'config.COMMUNITY_COORDS_GCJ'
    assert [first['longitude'], first['latitude']] == configured_coords[first['community']]
    assert 'risk_index' in first
    assert 'weather_hazard_score' in first
    assert 'burden_percentile' in first
    assert 'uncertainty_penalty' in first
    assert 'historical_component_available' in first
    assert 'risk_weights' in first
    assert 'risk_contributions' in first
    assert 'hazard_formula' in first
    assert 'svi_percentile' in first
    assert 'sir' in first
    assert 'ci_low' in first
    assert 'ci_high' in first
    assert 'uncertainty_index' in first
    assert 'hotspot_category' in first
    assert 'impact_bucket' in first
    assert 'likelihood_bucket' in first
    assert 'matrix_score' in first

    assert first['historical_component_available'] is True
    assert first['risk_weights'] == {'weather': 0.45, 'svi': 0.35, 'burden': 0.2}
    weights = first['risk_weights']
    recomputed = (
        weights['weather'] * first['weather_hazard_score']
        + weights['svi'] * first['svi_percentile']
        + weights['burden'] * first['burden_percentile']
    ) * first['uncertainty_penalty']
    assert abs(recomputed - first['risk_index']) <= 0.2
    contributions = first['risk_contributions']
    assert abs(contributions['weather'] - weights['weather'] * first['weather_hazard_score']) <= 0.02
    assert abs(contributions['svi'] - weights['svi'] * first['svi_percentile']) <= 0.02
    assert abs(contributions['burden'] - weights['burden'] * first['burden_percentile']) <= 0.02

    hazard_formula = first['hazard_formula']
    assert set(hazard_formula) == {
        'expression',
        'weather_rr',
        'vi',
        'baseline_visits',
        'excess',
        'efold',
        'hazard',
    }
    recomputed_excess = (
        max(hazard_formula['weather_rr'] - 1.0, 0.0)
        * hazard_formula['vi']
        * hazard_formula['baseline_visits']
    )
    recomputed_hazard = min(
        100.0,
        max(
            0.0,
            (1.0 - math.exp(-recomputed_excess / hazard_formula['efold'])) * 100.0,
        ),
    )
    assert math.isclose(hazard_formula['excess'], recomputed_excess, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(hazard_formula['hazard'], recomputed_hazard, rel_tol=0, abs_tol=1e-12)
    assert abs(hazard_formula['hazard'] - first['weather_hazard_score']) <= 0.05

    first_feature = payload['map_data']['features'][0]
    assert first_feature['properties']['hazard_formula'] == hazard_formula
    assert first_feature['geometry']['coordinates'] == configured_coords[first['community']]

    assert 'impact_likelihood_matrix' in payload
    matrix = payload['impact_likelihood_matrix']
    assert matrix['impact_levels'] == ['low', 'medium', 'high', 'very_high']
    assert matrix['likelihood_levels'] == ['low', 'medium', 'high', 'very_high']

    assert 'layers' in payload
    assert 'risk_index' in payload['layers']
    assert 'equity_stratification' in payload
    assert 'quartiles' in payload['equity_stratification']
    assert 'methodology' in payload
    assert len(payload['methodology']) >= 3

    summary = payload.get('summary', {})
    assert summary.get('window_days') == 30
    assert summary.get('total_communities') == 3
    assert summary.get('ranked_communities') == 3
    assert summary.get('unranked_communities') == 0
    assert summary.get('missing_coordinate_count') == 0
    assert summary.get('historical_component_available') is True
    assert 'equity_priority_count' in summary


def test_missing_vulnerability_fields_fail_closed_without_proxy_ranking(
    authenticated_client,
    db_session,
    monkeypatch,
):
    _seed_community_risk_data(db_session)

    import services.community_risk_service as risk_module
    from services.community_risk_cache import clear_local_community_risk_cache

    monkeypatch.setattr(risk_module, '_community_service', None)
    monkeypatch.setitem(
        authenticated_client.application.config,
        'COMMUNITY_COORDS_GCJ',
        {},
    )
    clear_local_community_risk_cache()

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={
            'analysis_date': '2025-10-30',
            'window_days': 30,
            'disease': '呼吸系统',
            'weather': _fresh_qweather(),
        },
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )

    assert response.status_code == 200
    payload = response.get_json()
    summary = payload['summary']
    assert summary['data_available'] is False
    assert summary['data_status'] == 'insufficient_vulnerability_data'
    assert summary['total_communities'] == 3
    assert summary['ranked_communities'] == 0
    assert summary['unranked_communities'] == 3
    assert summary['total_expected_excess'] is None
    assert summary['matched_records'] == 0
    assert summary['excluded_incomplete_profile_records'] > 0
    assert payload['map_data']['features'] == []
    assert payload['management_suggestions'] == []
    assert payload['impact_likelihood_matrix']['data_available'] is False
    assert payload['equity_stratification']['quartiles'] == []
    assert payload['equity_stratification']['priority_communities'] == []
    assert '数据不足，未参与排名' in payload['methodology'][0]

    for row in payload['rankings']:
        assert row['ranking_eligible'] is False
        assert row['rank'] is None
        assert row['data_status'] == 'insufficient_vulnerability_data'
        assert row['risk_score'] is None
        assert row['risk_index'] is None
        assert row['weather_hazard_score'] is None
        assert row['vulnerability_index'] is None
        assert row['expected_excess_visits'] is None
        assert row['hazard_formula'] is None
        assert row['risk_weights'] == {}
        assert row['risk_contributions'] == {}
        assert row['matrix_score'] is None
        assert row['coordinate_available'] is False
        assert row['coordinate_status'] == 'missing_in_config'
        assert set(row['missing_fields']) == {
            'green_space_ratio',
            'heat_island_index',
            'medical_accessibility',
            'baseline_visits',
        }


def test_complete_profile_without_config_coordinate_has_no_map_hotspot(
    authenticated_client,
    db_session,
    monkeypatch,
):
    db_session.add(MedicalRecord(
        patient_name='甲村坐标测试样本',
        gender='女',
        age=70,
        visit_time=datetime(2025, 10, 30, 8, 0, tzinfo=timezone.utc),
        disease_category='呼吸系统',
        community='甲村',
    ))
    db_session.commit()

    profiles = {
        '甲村': _complete_profile(
            '甲村',
            1200,
            0.33,
            0.12,
            db_coords=(118.8, 31.2),
        ),
    }
    _install_complete_service(authenticated_client, monkeypatch, profiles, {})

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={
            'analysis_date': '2025-10-30',
            'window_days': 30,
            'disease': '',
            'weather': _fresh_qweather(),
        },
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )

    assert response.status_code == 200
    payload = response.get_json()
    row = payload['rankings'][0]
    assert row['ranking_eligible'] is True
    assert row['rank'] == 1
    assert row['coordinate_available'] is False
    assert row['coordinate_status'] == 'missing_in_config'
    assert row['longitude'] is None
    assert row['latitude'] is None
    assert row['hotspot_category'] == '无坐标'
    assert payload['map_data']['features'] == []
    assert payload['map_data']['unmapped_communities'] == ['甲村']
    assert payload['summary']['ranked_missing_coordinate_count'] == 1


def test_all_unmatched_records_keep_historical_component_unavailable(
    authenticated_client,
    db_session,
    monkeypatch,
):
    communities = [
        Community(name='甲村', population=900, elderly_ratio=0.36, chronic_disease_ratio=0.14),
        Community(name='乙村', population=700, elderly_ratio=0.44, chronic_disease_ratio=0.18),
    ]
    db_session.add_all(communities)
    db_session.add(MedicalRecord(
        patient_name='未匹配样本',
        gender='女',
        age=72,
        visit_time=datetime(2026, 1, 10, 8, 0, tzinfo=timezone.utc),
        disease_category='呼吸系统',
        community='不存在于社区档案的村庄',
    ))
    db_session.commit()

    profiles = {
        '甲村': _complete_profile('甲村', 900, 0.36, 0.14),
        '乙村': _complete_profile('乙村', 700, 0.44, 0.18),
    }
    _install_complete_service(
        authenticated_client,
        monkeypatch,
        profiles,
        {
            '甲村': [116.201, 29.331],
            '乙村': [116.202, 29.332],
        },
    )

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={
            'analysis_date': '2026-01-10',
            'window_days': 30,
            'disease': '呼吸系统',
            'weather': _fresh_qweather(temperature=32, humidity=68, aqi=42),
        },
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )

    assert response.status_code == 200
    payload = response.get_json()
    summary = payload['summary']
    assert summary['total_records'] == 1
    assert summary['matched_records'] == 0
    assert summary['unmatched_records'] == 1
    assert summary['data_coverage_ratio'] == 0.0
    assert summary['historical_component_available'] is False
    assert summary['median_uncertainty_index'] is None

    for row in payload['rankings']:
        assert row['historical_component_available'] is False
        assert row['observed_cases'] is None
        assert row['sir'] is None
        assert row['ci_low'] is None
        assert row['ci_high'] is None
        assert row['smoothed_sir'] is None
        assert row['probability_exceed_baseline'] is None
        assert row['burden_percentile'] is None
        assert row['uncertainty_index'] is None
        assert row['uncertainty_penalty'] == 1.0
        assert row['risk_weights'] == {
            'weather': 0.5625,
            'svi': 0.4375,
            'burden': 0.0,
        }
        recomputed = (
            row['risk_weights']['weather'] * row['weather_hazard_score']
            + row['risk_weights']['svi'] * row['svi_percentile']
        )
        assert abs(recomputed - row['risk_index']) <= 0.2
