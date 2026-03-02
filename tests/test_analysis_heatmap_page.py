# -*- coding: utf-8 -*-
from datetime import date, datetime, timedelta, timezone

from core.db_models import Community, MedicalRecord, WeatherData


def _seed_heatmap_data(db_session):
    community = Community(name='测试社区')
    db_session.add(community)

    start_day = date(2025, 11, 1)
    for idx in range(56):
        day = start_day + timedelta(days=idx)
        temp = -2 + (idx % 12) * 3
        humidity = 35 + (idx % 6) * 10
        db_session.add(WeatherData(
            date=day,
            location='测试社区',
            temperature=float(temp),
            humidity=float(min(98, humidity))
        ))

        visits = 1
        if temp >= 28 and humidity >= 65:
            visits = 4
        elif temp <= 0:
            visits = 0

        for j in range(visits):
            db_session.add(MedicalRecord(
                patient_name=f'患者-{idx}-{j}',
                gender='男' if j % 2 == 0 else '女',
                age=70 if j % 2 == 0 else 42,
                visit_time=datetime(day.year, day.month, day.day, 9, 0, tzinfo=timezone.utc),
                disease_category='呼吸系统',
                community='测试社区'
            ))

    db_session.commit()


def test_heatmap_page_renders_new_controls(authenticated_client):
    response = authenticated_client.get('/analysis/heatmap')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '天气-疾病相关性热力图' in html
    assert 'name="lag_window"' in html
    assert 'name="stratum"' in html
    assert 'name="binning"' in html


def test_heatmap_page_post_with_rr_sections(authenticated_client, db_session):
    _seed_heatmap_data(db_session)

    response = authenticated_client.post(
        '/analysis/heatmap',
        data={
            'start_date': '2025-11-10',
            'end_date': '2025-12-20',
            'community': '测试社区',
            'disease': '呼吸系统',
            'stratum': 'elderly',
            'lag_window': '7',
            'binning': 'quantile',
            'min_days': '3',
            'csrf_token': 'test-csrf-token'
        },
        follow_redirects=True
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '单元详情（点击热力格）' in html
    assert '高风险格子（Top 5）' in html
    assert '基线日均病例' in html
    assert '温度 × 湿度 复合暴露矩阵（RR）' in html
