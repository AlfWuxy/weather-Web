"""验证成绩单移植保留生产访问控制和页面证据边界。"""
from flask import render_template

from services.model_validation_service import get_validation_scorecard


def test_model_quality_requires_login(client):
    response = client.get('/analysis/model-quality')
    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_model_quality_rejects_regular_account(authenticated_client):
    response = authenticated_client.get('/analysis/model-quality')
    assert response.status_code == 302
    assert 'retrospective-exploratory' not in response.text


def test_admin_model_quality_renders_aggregate_results(admin_client):
    response = admin_client.get('/analysis/model-quality')
    assert response.status_code == 200
    assert '275' in response.text
    assert '事前归档与前瞻验证' in response.text
    assert '线上天气健康模型的验证进展' in response.text
    assert '最近 14 天样本' not in response.text
    assert '报告文件：' not in response.text


def test_transparency_template_preserves_production_evidence_boundaries(app):
    # 公开路由的传参由账号组移植；此处独立验证合并后的完整模板。
    with app.test_request_context('/transparency'):
        body = render_template('transparency.html', validation_scorecard=get_validation_scorecard())
    assert '275' in body
    assert '敏感性结果' in body
    assert '原始资料的真实性、统计覆盖、单位及实际观察时间，由资料审核确认' in body
    assert '八个数值字段' in body
    assert 'NASA 2020–2024 年夏季晴空地表温度' in body
    assert '训练集准确率' in body
    assert '当前状态：研究存档。生产预测已停用。' in body
