# -*- coding: utf-8 -*-
import os
import tempfile

import pytest


TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "case_weather_test.db")
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
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/register").status_code == 200


def test_authenticated_pages(client):
    _login_as_guest(client)
    assert client.get("/dashboard").status_code == 200
    assert client.get("/health-assessment").status_code == 200
    assert client.get("/community-risk").status_code == 200
    assert client.get("/ai-qa").status_code == 200


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
