# -*- coding: utf-8 -*-
import math
import re
from datetime import datetime, timedelta, timezone

from core.db_models import Community, MedicalRecord


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


def test_community_risk_api_returns_extended_fields(authenticated_client, db_session):
    _seed_community_risk_data(db_session)

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={
            'analysis_date': '2025-10-30',
            'window_days': 30,
            'disease': '呼吸系统',
            'weather': {
                'temperature': 30,
                'humidity': 65,
                'aqi': 45,
                'data_source': 'QWeather',
                'is_mock': False,
            }
        },
        headers={'X-CSRF-Token': 'test-csrf-token'}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True

    assert 'rankings' in payload
    assert len(payload['rankings']) >= 1
    first = payload['rankings'][0]
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
    assert summary.get('total_communities', 0) >= 1
    assert summary.get('historical_component_available') is True
    assert 'equity_priority_count' in summary


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

    # 强制本次请求重新读取当前测试数据库中的社区与病例。
    monkeypatch.setattr(risk_module, '_community_service', None)
    clear_local_community_risk_cache()

    response = authenticated_client.post(
        '/api/community/risk-map-v2',
        json={
            'analysis_date': '2026-01-10',
            'window_days': 30,
            'disease': '呼吸系统',
            'weather': {
                'temperature': 32,
                'humidity': 68,
                'aqi': 42,
                'data_source': 'QWeather',
                'is_mock': False,
            },
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
