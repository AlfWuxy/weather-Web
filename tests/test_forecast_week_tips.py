# -*- coding: utf-8 -*-
"""7 天「本周建议」面向照护，不编造医院排班，并从 JSON 读取。"""


def test_forecast_week_tips_are_caregiver_copy_not_clinic_staffing():
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    recs = service._generate_forecast_recommendations(
        [
            {
                'date': '2026-06-06',
                'day_of_week': '周六',
                'risk_level': '红色预警',
                'extreme_events': [],
                'temperature': {'corrected': 36},
            },
            {
                'date': '2026-06-07',
                'day_of_week': '周日',
                'risk_level': '橙色预警',
                'extreme_events': [],
                'temperature': {'corrected': 22},
            },
        ],
        high_risk_days=4,
    )

    text = ' '.join(item.get('advice', '') for item in recs)
    assert recs
    assert '增派医护' not in text
    assert '值班人员' not in text
    assert '医疗资源配置' not in text
    assert any('提醒' in item.get('advice', '') or '照护' in item.get('advice', '') or '家人' in item.get('advice', '') for item in recs)


def test_forecast_week_tips_come_from_json():
    from core.forecast_copy import load_forecast_week_tips
    from services.forecast_service import ForecastService

    load_forecast_week_tips.cache_clear()
    copy = load_forecast_week_tips()
    service = ForecastService.__new__(ForecastService)
    recs = service._generate_forecast_recommendations(
        [
            {
                'date': '2026-06-01',
                'day_of_week': '周一',
                'risk_level': '绿色',
                'extreme_events': [],
                'temperature': {'corrected': 24},
            }
        ],
        high_risk_days=0,
    )

    assert copy['routine']['advice'] in {item['advice'] for item in recs}
    assert copy['routine']['category'] in {item['category'] for item in recs}


def test_role_action_cards_are_caregiver_copy_not_clinic_staffing():
    from services.forecast_service import ForecastService

    service = ForecastService.__new__(ForecastService)
    cards = service._build_role_action_cards(
        [
            {
                'probability_high_visits': 60,
                'composite_exposure': {'level': '高'},
                'cap_semantics': {'urgency': 'immediate'},
            }
        ],
        {'scenario_totals': {'baseline_total': 10, 'worst_case_total': 16}},
    )

    assert set(cards) == {'resident', 'doctor', 'community'}
    text = ' '.join(
        f"{item.get('title', '')} {item.get('action', '')}"
        for group in cards.values()
        for item in group
    )
    assert '村医排班' not in text
    assert '门急诊' not in text
    assert '社区资源调度' not in text
    assert '公众信息发布' not in text
    assert '家人' in text or '提醒' in text or '避暑' in text


def test_role_action_cards_come_from_json():
    from core.forecast_copy import load_forecast_week_tips
    from services.forecast_service import ForecastService

    load_forecast_week_tips.cache_clear()
    copy = load_forecast_week_tips()['role_cards']
    service = ForecastService.__new__(ForecastService)
    cards = service._build_role_action_cards(
        [{'probability_high_visits': 10, 'composite_exposure': {'level': '低'}}],
        {'scenario_totals': {'baseline_total': 8, 'worst_case_total': 8}},
    )

    resident_titles = {item['title'] for item in cards['resident']}
    doctor_titles = {item['title'] for item in cards['doctor']}
    community_titles = {item['title'] for item in cards['community']}
    assert copy['resident_daily']['title'] in resident_titles
    assert copy['doctor_prepare']['title'] in doctor_titles
    assert copy['community_cooling']['title'] in community_titles
    assert copy['community_info']['title'] in community_titles
