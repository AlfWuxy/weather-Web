# -*- coding: utf-8 -*-
"""P12：管理端病历列表 PHI 脱敏。"""
from utils.validators import mask_patient_name, mask_phi_text


def test_mask_patient_name_shapes():
    assert mask_patient_name('') == '—'
    assert mask_patient_name('王') == '*'
    assert mask_patient_name('王芳') == '王*'
    assert mask_patient_name('欧阳修') == '欧**'
    assert '张' in mask_patient_name('张三丰')
    assert '三' not in mask_patient_name('张三丰')


def test_mask_phi_text_full_hide():
    assert mask_phi_text('高血压伴心衰', keep=0) == '***'
    assert mask_phi_text('', keep=0) == '—'


def test_admin_records_list_hides_full_name(client, db_session):
    from core.db_models import MedicalRecord, User
    from datetime import datetime, timezone

    admin = User(username='phi_admin', role='admin')
    admin.set_password('testpass99')
    db_session.add(admin)
    db_session.add(
        MedicalRecord(
            patient_name='赵敏敏',
            gender='女性',
            age=72,
            visit_time=datetime.now(timezone.utc),
            department='内科',
            doctor='李医生',
            disease_category='循环系统',
            diagnosis='原发性高血压 3 级',
            community='测试村',
        )
    )
    db_session.commit()

    csrf = 'phi-csrf'
    with client.session_transaction() as sess:
        sess['_csrf_token'] = csrf
    client.post(
        '/login',
        data={'username': 'phi_admin', 'password': 'testpass99', 'csrf_token': csrf},
        follow_redirects=True,
    )
    resp = client.get('/admin/records')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '赵敏敏' not in body
    assert '赵**' in body or '赵*' in body
    assert '原发性高血压 3 级' not in body
    assert '***' in body
