# -*- coding: utf-8 -*-
"""社区风险页建议面向照护，不编造医院排班，并从 JSON 读取。"""


def test_community_suggestions_are_caregiver_copy_not_clinic_staffing():
    from services.community_risk_service import CommunityRiskService

    service = CommunityRiskService.__new__(CommunityRiskService)
    recs = service._generate_management_suggestions(
        [
            {
                'community': '甲村',
                'elderly_ratio': 0.52,
                'expected_excess_visits': 12,
            },
            {
                'community': '乙村',
                'elderly_ratio': 0.21,
                'expected_excess_visits': 4,
            },
            {
                'community': '丙村',
                'elderly_ratio': 0.18,
                'expected_excess_visits': 3,
            },
        ],
        {'temperature': 35},
    )

    text = ' '.join(item.get('advice', '') for item in recs)
    assert recs
    assert '增派医疗' not in text
    assert '门诊做好准备' not in text
    assert '常规健康管理工作' not in text
    assert any('提醒' in item.get('advice', '') or '避暑' in item.get('advice', '') or '家人' in item.get('advice', '') for item in recs)


def test_community_suggestions_come_from_json():
    from core.community_copy import load_community_action_tips
    from services.community_risk_service import CommunityRiskService

    load_community_action_tips.cache_clear()
    copy = load_community_action_tips()
    service = CommunityRiskService.__new__(CommunityRiskService)
    recs = service._generate_management_suggestions([], {'temperature': 22})

    assert copy['routine']['advice'] in {item['advice'] for item in recs}
    assert copy['heading'] == '优先行动'


def test_community_risk_page_uses_caregiver_action_heading(authenticated_client):
    html = authenticated_client.get('/community-risk').get_data(as_text=True)
    assert '管控建议' not in html
    assert '优先行动' in html
