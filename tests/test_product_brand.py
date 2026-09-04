# -*- coding: utf-8 -*-
"""产品品牌文案回归。"""


def test_action_and_health_pages_use_yilao_brand(authenticated_client):
    for path in ('/action', '/health-diary', '/medication-reminders'):
        response = authenticated_client.get(path)
        assert response.status_code == 200, path
        body = response.get_data(as_text=True)
        assert '宜老天气通' in body
        assert '天气健康风险预测系统' not in body


def test_ai_system_prompt_uses_product_name():
    from services.ai_question_service import AIQuestionService

    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {}
        content = b'{}'

        def json(self):
            return {
                'choices': [{'message': {'content': '注意补水。'}}]
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        captured['payload'] = json
        return FakeResponse()

    service = AIQuestionService('key', 'https://example.test', ['demo-model'])
    import services.ai_question_service as module

    original = module.requests.post
    module.requests.post = fake_post
    try:
        service.ask('今天热吗？', 'demo-model')
    finally:
        module.requests.post = original

    prompt = captured['payload']['messages'][0]['content']
    assert '宜老天气通' in prompt
    assert '天气健康风险预测系统' not in prompt
