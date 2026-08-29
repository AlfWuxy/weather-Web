# -*- coding: utf-8 -*-
import re
from datetime import datetime, timedelta, timezone

from core.db_models import Community, MedicalRecord


STATIC_CLINICAL_NULL_FIELDS = (
    'risk_index',
    'weather_hazard_score',
    'expected_excess_visits',
    'observed_cases',
    'expected_cases',
    'sir',
    'ci_low',
    'ci_high',
    'smoothed_sir',
    'probability_exceed_baseline',
    'burden_percentile',
    'uncertainty_index',
    'hotspot_z',
    'hotspot_p',
    'matrix_score',
)


def _complete_profile(name, population, elderly_ratio, chronic_disease_ratio):
    """构造通过正式画像完整性门的测试档案。"""
    return {
        'id': None,
        'name': name,
        'location': f'测试地点{name}',
        'latitude': None,
        'longitude': None,
        'coordinate_available': False,
        'coordinate_source': None,
        'coordinate_status': 'missing_config_coordinate',
        'coordinate_message': '测试社区未配置公开地图坐标。',
        'population': population,
        'elderly_ratio': elderly_ratio,
        'chronic_disease_ratio': chronic_disease_ratio,
        'green_space_ratio': 0.18,
        'heat_island_index': 0.47,
        'medical_accessibility': 0.62,
        'baseline_visits': 6.0,
        'uses_proxy_values': False,
    }


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


def _trusted_weather(temperature=30.0, **overrides):
    payload = {
        'temperature': temperature,
        'temperature_max': temperature + 3,
        'temperature_min': temperature - 5,
        'humidity': 65,
        'pressure': 1005,
        'wind_speed': 1.8,
        'weather_condition': '晴',
        'aqi': 45,
        'data_source': 'QWeather',
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'quality_version': 1,
        'is_mock': False,
    }
    payload.update(overrides)
    return payload


def test_community_risk_page_has_academic_sections(authenticated_client):
    response = authenticated_client.get('/community-risk')
    assert response.status_code == 200

    html = response.get_data(as_text=True)
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


def test_community_risk_api_routes_incomplete_profiles_to_static_screening(
    authenticated_client,
    db_session,
    monkeypatch,
):
    import services.community_risk_service as risk_module
    from services.community_risk_cache import clear_local_community_risk_cache

    _seed_community_risk_data(db_session)
    monkeypatch.setattr(risk_module, '_community_service', None)
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda city: (_trusted_weather(), True),
    )
    clear_local_community_risk_cache()

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={
            'analysis_date': '2025-10-30',
            'window_days': 30,
            'disease': '呼吸系统',
        },
        headers={'X-CSRF-Token': 'test-csrf-token'}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True

    assert payload['ranking_mode'] == 'exploratory_geospatial_screening'
    assert len(payload['rankings']) == 16
    for row in payload['rankings']:
        assert row['rank'] is not None
        assert row['ranking_eligible'] is True
        assert row['data_status'] == 'exploratory_geospatial_screening'
        assert row['screening_score'] is not None
        assert row['risk_weights'] == {}
        assert row['historical_component_available'] is False
        for field in STATIC_CLINICAL_NULL_FIELDS:
            assert row[field] is None
    assert len(payload['map_data']['features']) == 16
    assert len(payload['layers']['screening_score']) == 16
    assert payload['layers']['risk_index'] == []
    assert payload['layers']['uncertainty'] == []
    assert payload['layers']['hotspot'] == []
    assert payload['equity_stratification']['quartiles'] == []
    summary = payload['summary']
    assert summary['data_available'] is True
    assert summary['data_status'] == 'exploratory_geospatial_screening'
    assert summary['ranked_communities'] == 16
    assert summary['unranked_communities'] == 0
    assert summary['total_expected_excess'] is None
    assert summary['historical_component_available'] is False
    assert summary['total_records'] is None
    assert summary['matched_records'] is None
    assert summary['unmatched_records'] is None
    clear_local_community_risk_cache()


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

    import services.community_risk_service as risk_module
    from services.community_risk_cache import clear_local_community_risk_cache

    service = risk_module.CommunityRiskService()
    service.community_profiles = {
        '甲村': _complete_profile('甲村', 900, 0.36, 0.14),
        '乙村': _complete_profile('乙村', 700, 0.44, 0.18),
    }
    for profile in service.community_profiles.values():
        profile['profile_readiness'] = service._profile_readiness(profile)
    service.community_profile_status = {
        'available': True,
        'code': 'available',
        'source': 'test_full_profiles',
        'message': '测试完整画像已加载。',
        'total_count': 2,
        'eligible_count': 2,
    }
    monkeypatch.setattr(service, '_load_community_profiles', lambda: None)

    class StubDLNM:
        def calculate_rr(self, temperature, lag_temperatures=None):
            del temperature, lag_temperatures
            return 1.8, {}

    monkeypatch.setattr(
        'services.dlnm_risk_service.get_dlnm_service',
        lambda: StubDLNM(),
    )
    monkeypatch.setattr(
        risk_module,
        'get_community_service',
        lambda: service,
    )
    monkeypatch.setattr(
        'services.api_service.get_weather_with_cache',
        lambda city: (_trusted_weather(32, humidity=68, aqi=42), True),
    )
    clear_local_community_risk_cache()

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={
            'analysis_date': '2026-01-10',
            'window_days': 30,
            'disease': '呼吸系统',
        },
        headers={'X-CSRF-Token': 'test-csrf-token'},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ranking_mode'] is None
    summary = payload['summary']
    assert summary['data_status'] == 'available'
    assert summary['ranked_communities'] == 2
    assert summary['total_records'] == 1
    assert summary['matched_records'] == 0
    assert summary['unmatched_records'] == 1
    assert summary['data_coverage_ratio'] == 0.0
    assert summary['historical_component_available'] is False
    assert summary['median_uncertainty_index'] is None

    for row in payload['rankings']:
        assert row['ranking_eligible'] is True
        assert row['risk_index'] is not None
        assert row['expected_excess_visits'] is not None
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
        assert row['risk_weights']['burden'] == 0.0
