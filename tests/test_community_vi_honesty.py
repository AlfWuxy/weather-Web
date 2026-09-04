# -*- coding: utf-8 -*-
"""社区脆弱性指数不得用医疗可达性 50/60、环境 50/70 顶上。"""
from services.health_risk_service import HealthRiskService


def test_vulnerability_index_does_not_invent_medical_or_env_scores():
    result = HealthRiskService().calculate_community_vulnerability_index({
        'elderly_ratio': 0.4,
        'chronic_disease_ratio': 0.2,
    })

    assert result['breakdown']['medical_score'] in (0, 0.0, None)
    assert result['breakdown']['env_score'] in (0, 0.0, None)
    assert result['breakdown']['medical_score'] != 10.0
    assert result['breakdown']['env_score'] != 7.5


def test_admin_add_community_does_not_store_placeholder_vulnerability(
    admin_client,
    db_session,
):
    from core.db_models import Community

    response = admin_client.post(
        '/admin/community/add',
        data={
            'name': '新村',
            'location': '都昌',
            'csrf_token': 'test-csrf-token',
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    community = Community.query.filter_by(name='新村').first()
    assert community is not None
    assert community.vulnerability_index is None
    assert community.risk_level in (None, '')
