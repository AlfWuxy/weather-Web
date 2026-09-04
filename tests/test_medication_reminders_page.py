# -*- coding: utf-8 -*-
"""用药提醒页面回归测试。"""


def _login_as(client, user_id: int, csrf_token='test-csrf-token'):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
        session['_csrf_token'] = csrf_token


def test_medication_reminders_page_shows_member_name(client, db_session):
    from core.db_models import FamilyMember, MedicationReminder, User

    user = User(username='med_name_user', role='user')
    user.set_password('testpass')
    db_session.add(user)
    db_session.flush()
    member = FamilyMember(user_id=user.id, name='王奶奶', relation='母亲', age=80, gender='女性')
    db_session.add(member)
    db_session.flush()
    db_session.add(MedicationReminder(
        user_id=user.id,
        member_id=member.id,
        medicine_name='降压药',
        dosage='1片',
        frequency='每日一次',
        time_of_day='08:00',
    ))
    db_session.commit()
    _login_as(client, user.id)

    response = client.get('/medication-reminders')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '王奶奶' in body
    assert 'FamilyMember' not in body
    assert '宜老天气通' in body
    assert '天气健康风险预测系统' not in body
