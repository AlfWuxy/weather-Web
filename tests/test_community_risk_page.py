# -*- coding: utf-8 -*-
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
    assert '社区健康风险地图' in html
    assert '地图图层' in html
    assert 'Impact × Likelihood' in html
    assert '公平性分层（脆弱社区优先）' in html
    assert 'id="layerSelect"' in html
    assert '社区风险明细' in html


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
    assert 'svi_percentile' in first
    assert 'sir' in first
    assert 'ci_low' in first
    assert 'ci_high' in first
    assert 'uncertainty_index' in first
    assert 'hotspot_category' in first
    assert 'impact_bucket' in first
    assert 'likelihood_bucket' in first
    assert 'matrix_score' in first

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
    assert 'equity_priority_count' in summary
