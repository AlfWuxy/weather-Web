# -*- coding: utf-8 -*-
from datetime import date, datetime, timedelta, timezone

from core.db_models import Community, MedicalRecord


def _seed_community_compare_data(db_session):
    communities = [
        Community(name='甲村', population=1200, vulnerability_index=0.32, risk_level='低'),
        Community(name='乙村', population=420, vulnerability_index=0.74, risk_level='高'),
        Community(name='丙村', population=860, vulnerability_index=0.49, risk_level='中'),
        Community(name='丁村', vulnerability_index=0.58, risk_level='中'),
    ]
    db_session.add_all(communities)

    start_day = date(2025, 9, 1)
    for idx in range(72):
        day = start_day + timedelta(days=idx)
        community_counts = {
            '甲村': 1,
            '乙村': 3 if idx % 3 != 0 else 2,
            '丙村': 1 if idx % 4 == 0 else 0,
            '丁村': 1 if idx % 10 == 0 else 0
        }
        for community, visits in community_counts.items():
            for visit_idx in range(visits):
                db_session.add(MedicalRecord(
                    patient_name=f'{community}-样本-{idx}-{visit_idx}',
                    gender='男' if (idx + visit_idx) % 2 == 0 else '女',
                    age=69 if community == '乙村' else (52 if visit_idx % 2 == 0 else 36),
                    visit_time=datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc),
                    disease_category='呼吸系统' if idx % 2 == 0 else '心血管',
                    community=community
                ))

    # 补充一个仅在病历中出现、但不在 communities 表中的社区。
    db_session.add(MedicalRecord(
        patient_name='外来样本',
        gender='男',
        age=61,
        visit_time=datetime(2025, 10, 25, 9, 30, tzinfo=timezone.utc),
        disease_category='呼吸系统',
        community='外来村'
    ))

    db_session.commit()


def test_community_compare_get_shows_academic_controls(admin_client):
    response = admin_client.get('/analysis/community-compare')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '社区对比分析' in html
    assert 'name="stratum"' in html
    assert 'name="smoothing_alpha"' in html
    assert 'name="min_days"' in html


def test_community_compare_post_renders_risk_sections(admin_client, db_session):
    _seed_community_compare_data(db_session)

    response = admin_client.post(
        '/analysis/community-compare',
        data={
            'start_date': '2025-09-10',
            'end_date': '2025-11-05',
            'disease': '呼吸系统',
            'stratum': 'all',
            'min_days': '3',
            'smoothing_alpha': '5',
            'top_n': '12',
            'csrf_token': 'test-csrf-token'
        },
        follow_redirects=True
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '社区间门诊记录 O/E 排序' in html
    assert '漏斗图（Funnel Plot）' in html
    assert '社区对比明细（人口天数校正与不确定性）' in html
    assert '加 α 平滑 O/E' in html
    assert '标准化发病比' not in html
    assert 'P90/P10' in html
    assert '甲村' in html
    assert '乙村' in html
