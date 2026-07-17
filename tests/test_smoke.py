# -*- coding: utf-8 -*-
import os
import tempfile

import pytest


# 每个 pytest 进程使用独立数据库，避免未来并行 CI 互相删表。
TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f"case_weather_test_{os.getpid()}.db")
os.environ["DATABASE_URI"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["QWEATHER_KEY"] = ""
os.environ["AMAP_KEY"] = ""
os.environ["SILICONFLOW_API_KEY"] = ""
os.environ["SECRET_KEY"] = "test-secret-key-for-smoke-tests-123456"
os.environ["PAIR_TOKEN_PEPPER"] = "test-pair-token-pepper-1234567890"
os.environ["DEBUG"] = "true"

from app import app, db, User  # noqa: E402


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
    with app.test_client() as client:
        yield client
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)


def _set_csrf_token(client, token="test-token"):
    with client.session_transaction() as session:
        session["_csrf_token"] = token
    return token


def _login_as_guest(client):
    response = client.get("/guest", follow_redirects=True)
    assert response.status_code == 200


def test_db_roundtrip(client):
    with app.app_context():
        user = User(username="smoke_user")
        user.set_password("smoke_password")
        db.session.add(user)
        db.session.commit()
        found = User.query.filter_by(username="smoke_user").first()
        assert found is not None


def test_public_pages(client):
    home = client.get("/")
    login = client.get("/login")
    assert home.status_code == 200
    assert login.status_code == 200
    assert client.get("/register").status_code == 200
    login_body = login.get_data(as_text=True)
    assert "查看风险提醒" in login_body
    assert "沉淀试点数据" not in login_body
    assert "管理员账号" not in login_body

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert robots.mimetype == "text/plain"
    assert "User-agent: *" in robots.get_data(as_text=True)
    assert "Disallow: /admin" in robots.get_data(as_text=True)

    health = client.get('/healthz')
    assert health.status_code == 200
    assert health.get_json() == {'status': 'ok'}
    assert health.headers['Cache-Control'] == 'no-store'


def test_authenticated_pages(client):
    _login_as_guest(client)
    assert client.get("/dashboard").status_code == 200
    assert client.get("/health-assessment").status_code == 200
    assert client.get("/community-risk").status_code == 200
    ai_response = client.get("/ai-qa")
    assert ai_response.status_code == 200
    ai_body = ai_response.get_data(as_text=True)
    assert "查询天气信息和通用行动建议" in ai_body
    assert "全站悬浮助手" not in ai_body


def test_key_api_endpoints(client):
    _login_as_guest(client)
    csrf_token = _set_csrf_token(client)

    response = client.get("/api/weather/current")
    assert response.status_code == 200
    payload = response.get_json()
    assert "success" in payload

    response = client.get("/api/community/list")
    assert response.status_code == 200
    payload = response.get_json()
    assert "success" in payload
    assert "communities" in payload

    response = client.get("/api/chronic/rules-version")
    assert response.status_code == 200
    payload = response.get_json()
    assert "success" in payload
    assert "version" in payload

    response = client.get("/api/ml/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert "success" in payload
    assert "status" in payload

    response = client.post(
        "/api/forecast/7day",
        json={"forecast_temps": [15, 16, 17, 18, 19, 20, 21]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert "success" in payload
