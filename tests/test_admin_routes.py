# -*- coding: utf-8 -*-
"""Smoke tests for admin routes.

These routes are easy to accidentally break due to DB dialect detection or
Flask-SQLAlchemy/SQLAlchemy version differences.
"""
from datetime import datetime


def _login_as(client, user_id: int):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True


def test_admin_dashboard_renders(client, db_session):
    from core.db_models import MedicalRecord, User

    admin = User(username='admin_test', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.add_all([
        MedicalRecord(
            patient_name='甲',
            visit_time=datetime(2024, 1, 15, 8, 0),
            disease_category='呼吸系统疾病',
        ),
        MedicalRecord(
            patient_name='乙',
            visit_time=datetime(2024, 3, 20, 8, 0),
            disease_category='心血管疾病',
        ),
    ])
    db_session.commit()

    _login_as(client, admin.id)
    resp = client.get('/admin')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '管理后台仪表板' in body
    assert '数据时间范围：2024-01 至 2024-03（2 个有记录月份）' in body
    assert '当前展示病例数最多的 2 个分类' in body
    assert '2023-12 至 2025-01（共13个月）' not in body
    assert '共48种疾病分类' not in body


def test_admin_statistics_renders(client, db_session):
    from core.db_models import User

    admin = User(username='admin_stats', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.commit()

    _login_as(client, admin.id)
    resp = client.get('/admin/statistics')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '统计分析' in body
    assert '暂无可计算数据' in body
    assert '本页尚未接入按同一日期对齐的天气与病例序列' in body
    assert '相关系数分析（基于历史数据）' not in body
    assert 'data: [35, 75, 65, 85, 70]' not in body
    assert 'min="2023-12-01"' not in body
