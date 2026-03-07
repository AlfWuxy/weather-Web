# -*- coding: utf-8 -*-
from datetime import date, datetime, timedelta, timezone

from core.db_models import Community, MedicalRecord, WeatherAlert


def _seed_alerts_accuracy_data(db_session):
    db_session.add(Community(name='测试社区', population=1300))

    start_day = date(2025, 10, 1)
    spike_indexes = {12, 24, 36, 48}
    for idx in range(60):
        day = start_day + timedelta(days=idx)
        visits = 5 if idx in spike_indexes else 1
        for visit_idx in range(visits):
            db_session.add(MedicalRecord(
                patient_name=f'病例-{idx}-{visit_idx}',
                gender='男' if visit_idx % 2 == 0 else '女',
                age=67 if visit_idx % 2 == 0 else 43,
                visit_time=datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc),
                disease_category='呼吸系统',
                community='测试社区'
            ))

    alert_specs = [
        (start_day + timedelta(days=11), '高温', '黄色预警', '预计升温，可能影响健康'),
        (start_day + timedelta(days=23), '高温', '橙色预警', '预计持续高温，较可能发生风险'),
        (start_day + timedelta(days=30), '降温', '蓝色预警', '短时降温提醒'),
        (start_day + timedelta(days=47), '高温', '红色预警', '已发生高温过程'),
        (start_day + timedelta(days=52), '降温', '蓝色预警', '预计轻度降温')
    ]
    for alert_day, alert_type, level, desc in alert_specs:
        db_session.add(WeatherAlert(
            alert_date=datetime(alert_day.year, alert_day.month, alert_day.day, 7, 20, tzinfo=timezone.utc),
            location='测试社区',
            alert_type=alert_type,
            alert_level=level,
            description=desc,
            affected_communities='["测试社区"]',
            disease_correlation='{}'
        ))

    db_session.commit()


def test_alerts_accuracy_get_shows_academic_sections(admin_client):
    response = admin_client.get('/alerts/accuracy')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '预警准确率统计' in html
    assert 'name="threshold_q"' in html
    assert 'name="follow_days"' in html
    assert '混淆矩阵（2×2）' in html


def test_alerts_accuracy_post_renders_verification_metrics(admin_client, db_session):
    _seed_alerts_accuracy_data(db_session)

    response = admin_client.post(
        '/alerts/accuracy',
        data={
            'start_date': '2025-10-01',
            'end_date': '2025-11-30',
            'location': '测试社区',
            'follow_days': '3',
            'threshold_q': '0.95',
            'min_days': '5',
            'csrf_token': 'test-csrf-token'
        },
        follow_redirects=True
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'CSI（Critical Success Index）' in html
    assert '可靠性图（Reliability Diagram）' in html
    assert '阈值敏感性（分位数）' in html
    assert 'Brier Score' in html
    assert 'ROC AUC' in html
    assert '周度校准跟踪（自动）' in html
    assert 'CAP certainty 校准表' in html
