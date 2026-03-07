# -*- coding: utf-8 -*-
from datetime import date, datetime, timedelta, timezone

from core.db_models import Community, MedicalRecord, WeatherAlert


def _seed_alerts_history_data(db_session):
    db_session.add(Community(name='测试社区', population=1200))

    start_day = date(2025, 11, 1)
    spike_indexes = {10, 18, 26, 34}
    for idx in range(45):
        day = start_day + timedelta(days=idx)
        visits = 1
        if idx in spike_indexes:
            visits = 5

        for visit_idx in range(visits):
            db_session.add(MedicalRecord(
                patient_name=f'病例-{idx}-{visit_idx}',
                gender='男' if visit_idx % 2 == 0 else '女',
                age=68 if visit_idx % 2 == 0 else 44,
                visit_time=datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc),
                disease_category='呼吸系统',
                community='测试社区'
            ))

    alert_specs = [
        (start_day + timedelta(days=9), '高温', '黄色预警', '预计升温'),
        (start_day + timedelta(days=17), '高温', '橙色预警', '预计持续高温'),
        (start_day + timedelta(days=5), '降温', '蓝色预警', '短时降温提醒'),
        (start_day + timedelta(days=30), '降温', '蓝色预警', '弱冷空气影响')
    ]
    for alert_day, alert_type, level, desc in alert_specs:
        db_session.add(WeatherAlert(
            alert_date=datetime(alert_day.year, alert_day.month, alert_day.day, 7, 30, tzinfo=timezone.utc),
            location='测试社区',
            alert_type=alert_type,
            alert_level=level,
            description=desc,
            affected_communities='["测试社区"]',
            disease_correlation='{}'
        ))

    db_session.commit()


def test_alerts_history_get_shows_new_controls(admin_client):
    response = admin_client.get('/alerts/history')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '预警历史记录' in html
    assert 'name="follow_days"' in html
    assert 'name="threshold_q"' in html
    assert 'name="outcome"' in html


def test_alerts_history_post_renders_verification_sections(admin_client, db_session):
    _seed_alerts_history_data(db_session)

    response = admin_client.post(
        '/alerts/history',
        data={
            'start_date': '2025-11-01',
            'end_date': '2025-12-10',
            'location': '测试社区',
            'follow_days': '3',
            'threshold_q': '0.95',
            'min_days': '5',
            'outcome': 'all',
            'csrf_token': 'test-csrf-token'
        },
        follow_redirects=True
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '命中率（Alert Hit Rate）' in html
    assert '事件命中率（POD）' in html
    assert '预警时间线（发布 vs 实况）' in html
    assert 'CAP语义' in html
    assert '命中' in html
    assert '空报' in html
