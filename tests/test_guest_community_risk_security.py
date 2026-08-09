# -*- coding: utf-8 -*-
"""游客访问社区风险页时不得进入真实用户数据查询链路。"""


def test_guest_community_risk_redirects_before_service_call(client, monkeypatch):
    assert client.get('/guest', follow_redirects=False).status_code == 302

    def _should_not_run():
        raise AssertionError('游客请求不得进入社区风险服务层')

    monkeypatch.setattr(
        'blueprints.user.user_service.community_risk',
        _should_not_run,
    )

    response = client.get('/community-risk', follow_redirects=False)

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/risk')


def test_guest_navigation_only_links_to_public_risk(client):
    assert client.get('/guest', follow_redirects=False).status_code == 302

    body = client.get('/').get_data(as_text=True)

    assert 'href="/community-risk" data-nav-key="community-risk"' not in body
    assert 'href="/risk" data-nav-key="community-risk"' in body
    assert 'href="/risk" class="yl-role-card variant-doctor"' in body


def test_anonymous_community_risk_still_requires_login(client):
    response = client.get('/community-risk', follow_redirects=False)

    assert response.status_code == 302
    assert '/login' in response.headers['Location']
