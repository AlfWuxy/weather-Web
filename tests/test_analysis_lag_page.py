# -*- coding: utf-8 -*-
from datetime import date, datetime, timedelta, timezone

from core.db_models import Community, MedicalRecord, WeatherData


def _seed_lag_data(db_session):
    community = Community(name='滞后社区')
    db_session.add(community)

    start_day = date(2025, 10, 1)
    temps = []
    for idx in range(90):
        day = start_day + timedelta(days=idx)
        temp = 8 + (idx % 18) * 1.7
        humidity = 45 + (idx % 7) * 6
        temps.append(temp)

        db_session.add(WeatherData(
            date=day,
            location='滞后社区',
            temperature=float(temp),
            humidity=float(min(95, humidity))
        ))

        visits = 1
        if idx >= 2 and temps[idx - 2] >= 30:
            visits = 4
        elif idx >= 5 and temps[idx - 5] <= 12:
            visits = 2

        for j in range(visits):
            db_session.add(MedicalRecord(
                patient_name=f'滞后样本-{idx}-{j}',
                gender='男' if j % 2 == 0 else '女',
                age=70 if j % 2 == 0 else 45,
                visit_time=datetime(day.year, day.month, day.day, 8, 30, tzinfo=timezone.utc),
                disease_category='呼吸系统',
                community='滞后社区'
            ))

    db_session.commit()


def test_lag_page_get_shows_new_controls(authenticated_client):
    response = authenticated_client.get('/analysis/lag')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '滞后效应可视化' in html
    assert 'name="max_lag"' in html
    assert 'name="stratum"' in html
    assert 'name="disease"' in html


def test_lag_page_post_renders_academic_sections(authenticated_client, db_session):
    _seed_lag_data(db_session)

    response = authenticated_client.post(
        '/analysis/lag',
        data={
            'start_date': '2025-10-10',
            'end_date': '2025-12-20',
            'community': '滞后社区',
            'disease': '呼吸系统',
            'stratum': 'all',
            'max_lag': '14',
            'min_days': '3',
            'csrf_token': 'test-csrf-token'
        },
        follow_redirects=True
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Lag-Response 相关曲线（温度 vs 病例）' in html
    assert 'Lag Slices（热暴露/冷暴露 RR）' in html
    assert '累计窗口效应（0~k 天平均温度）' in html
    assert '事件语义映射（CAP）' in html
