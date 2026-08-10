# -*- coding: utf-8 -*-
"""Web AI 正式关闭与第三方端点边界测试。"""

import pytest


def test_web_ai_is_hidden_and_authenticated_api_fails_closed(client, authenticated_client):
    page = client.get('/')
    body = page.get_data(as_text=True)
    assert 'ai-floating-chat.css' not in body
    assert 'ai-floating-chat.js' not in body
    assert 'id="ai-floating-chat"' not in body

    with authenticated_client.session_transaction() as session:
        session['_csrf_token'] = 'web-ai-disabled-csrf'
    response = authenticated_client.post(
        '/api/v1/ai/ask',
        json={'question': '测试问题', 'model': 'unused'},
        headers={'X-CSRF-Token': 'web-ai-disabled-csrf'},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    'api_base',
    (
        'http://api.siliconflow.cn/v1',
        'https://api.siliconflow.cn.evil.example/v1',
        'https://user@api.siliconflow.cn/v1',
        'https://api.siliconflow.cn:8443/v1',
        'https://api.siliconflow.cn/v1/chat',
        'https://api.siliconflow.cn/v1?next=evil',
        'https://api.siliconflow.cn/v1#fragment',
    ),
)
def test_ai_service_rejects_nonofficial_api_base(api_base):
    from services.ai_question_service import AIQuestionService

    with pytest.raises(ValueError, match='官方 HTTPS'):
        AIQuestionService('secret', api_base, ['model'])


def test_ai_service_disables_redirect_following(monkeypatch):
    from services import ai_question_service as module

    calls = []

    class RedirectResponse:
        status_code = 307
        headers = {'Location': 'https://evil.example/collect'}
        text = ''

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return RedirectResponse()

    monkeypatch.setattr(module.requests, 'post', fake_post)
    service = module.AIQuestionService(
        'server-secret',
        'https://api.siliconflow.cn/v1',
        ['model'],
        retries=1,
    )

    with pytest.raises(RuntimeError, match='AI服务暂不可用'):
        service.ask('不会跟随重定向', 'model')

    assert len(calls) == 1
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][0][0] == 'https://api.siliconflow.cn/v1/chat/completions'
