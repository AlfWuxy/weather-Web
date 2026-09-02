# -*- coding: utf-8 -*-


def test_pair_page_explains_confirm_deadline_and_backup_escalate(authenticated_client):
    response = authenticated_client.get('/pairs')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '每日 20:00 前确认' in html
    assert '2 小时未确认会转备用联系人' in html
