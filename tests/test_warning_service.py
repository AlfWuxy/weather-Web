# -*- coding: utf-8 -*-


def test_warning_service_no_key_returns_empty(app):
    from services.warning_service import get_qweather_warnings

    with app.app_context():
        app.config["QWEATHER_KEY"] = ""
        warnings = get_qweather_warnings("116.20,29.27")
        assert warnings == []


def test_warning_service_parses_payload(app, monkeypatch):
    from services.warning_service import get_qweather_warnings

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "code": "200",
                "warning": [
                    {
                        "title": "高温黄色预警",
                        "typeName": "高温",
                        "level": "黄色",
                        "text": "请注意防暑降温",
                        "startTime": "2026-02-09T08:00+08:00",
                        "endTime": "2026-02-09T20:00+08:00"
                    }
                ],
            }

    monkeypatch.setattr("services.warning_service.requests.get", lambda *args, **kwargs: FakeResp())

    with app.app_context():
        app.config["QWEATHER_KEY"] = "x"
        app.config["QWEATHER_API_BASE"] = "https://example.com/v7"

        warnings = get_qweather_warnings("116.20,29.27")
        assert len(warnings) == 1
        item = warnings[0]
        assert item["title"] == "高温黄色预警"
        assert item["type"] == "高温"
        assert "text" in item
        assert item["severity"] == "Minor"
        assert item["certainty"] == "Likely"
        assert item["urgency"] == "Expected"
