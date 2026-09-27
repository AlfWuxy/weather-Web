# -*- coding: utf-8 -*-
"""存活探测与待处理限流默认值。"""


def test_healthz_ok_without_weather(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_pending_rate_limit_defaults_cover_nat_polling(app):
    assert app.config["RATE_LIMIT_MP_PENDING_IP"] == "400 per minute"
    assert app.config["RATE_LIMIT_MP_PENDING_USER"] == "360 per minute"
    assert app.config.get("HELP_NOTIFY_SANDBOX") is True
