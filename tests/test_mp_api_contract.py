# -*- coding: utf-8 -*-
"""Mini Program API contract vs client assumptions."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def _create_mp_token(db_session, username):
    from core.db_models import User
    from core.usage import create_api_token

    user = User(username=username, role="user")
    user.set_password("pw123456")
    db_session.add(user)
    db_session.commit()
    return user, create_api_token(user.id, name="test")


def _auth(plain):
    return {"Authorization": f"Bearer {plain}"}


def _stub_mp_weather(monkeypatch, warnings=None, weather=None):
    payload = weather or {
        "temperature": 28,
        "temperature_max": 32,
        "temperature_min": 24,
        "data_source": "QWeather",
        "is_mock": False,
    }
    monkeypatch.setattr(
        "blueprints.mp_api.resolve_location",
        lambda _label: {"location_code": "101240201", "provider": "QWeather"},
    )
    monkeypatch.setattr(
        "blueprints.mp_api.get_weather_with_cache",
        lambda _location: (payload, False),
    )
    monkeypatch.setattr(
        "blueprints.mp_api.get_qweather_warnings",
        lambda _code: list(warnings or []),
    )


def test_mp_elders_patch_persists_location_and_chronic_list_and_returns_data(
    app, client, db_session, monkeypatch
):
    with app.app_context():
        _user, plain = _create_mp_token(db_session, "mp_patch_owner")
    _stub_mp_weather(monkeypatch)

    created = client.post(
        "/mp/api/v1/elders",
        json={
            "name": "妈妈",
            "relation": "母亲",
            "location_query": "都昌",
            "chronic_diseases": ["高血压"],
        },
        headers=_auth(plain),
    )
    assert created.status_code == 200
    pair_id = created.get_json()["data"]["pair_id"]

    resp = client.patch(
        f"/mp/api/v1/elders/{pair_id}",
        json={"location_query": "上海市", "chronic_diseases": ["高血压", "糖尿病"]},
        headers=_auth(plain),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert "data" in body
    assert isinstance(body["data"], dict)
    assert body["data"]["pair_id"] == pair_id
    assert isinstance(body["data"]["pair_id"], int)
    assert body["data"]["location_query"] == "上海市"
    assert body["data"]["chronic_diseases"] == ["高血压", "糖尿病"]
    assert isinstance(body["data"]["chronic_diseases"], list)

    listed = client.get("/mp/api/v1/elders", headers=_auth(plain)).get_json()
    assert listed["success"] is True
    item = listed["data"][0]
    assert item["location_query"] == "上海市"
    assert item["member"]["chronic_diseases"] == ["高血压", "糖尿病"]
    assert isinstance(item["member"]["chronic_diseases"], list)
    assert "has_official_warning" in item["today"]
    assert item["today"]["has_official_warning"] is False


def test_mp_elders_patch_other_user_returns_404(app, client, db_session, monkeypatch):
    with app.app_context():
        _owner, owner_token = _create_mp_token(db_session, "mp_patch_owner2")
        _other, other_token = _create_mp_token(db_session, "mp_patch_other")
    _stub_mp_weather(monkeypatch)

    created = client.post(
        "/mp/api/v1/elders",
        json={"name": "爸爸", "location_query": "都昌"},
        headers=_auth(owner_token),
    )
    pair_id = created.get_json()["data"]["pair_id"]

    resp = client.patch(
        f"/mp/api/v1/elders/{pair_id}",
        json={"location_query": "杭州市"},
        headers=_auth(other_token),
    )
    assert resp.status_code == 404
    assert resp.is_json
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"] == "not_found"

    listed = client.get("/mp/api/v1/elders", headers=_auth(owner_token)).get_json()
    assert listed["data"][0]["location_query"] == "都昌"


def test_mp_events_template_copy_returns_200(app, client, db_session):
    from core.db_models import UsageEvent

    with app.app_context():
        user, plain = _create_mp_token(db_session, "mp_copy_user")
        user_id = user.id

    resp = client.post(
        "/mp/api/v1/events",
        json={"event_type": "template_copy", "pair_id": None, "meta": {"trigger": "heat"}},
        headers=_auth(plain),
    )
    assert resp.status_code == 200
    assert resp.is_json
    body = resp.get_json()
    assert body["success"] is True
    assert "data" in body
    assert body["data"] == {}

    with app.app_context():
        assert (
            UsageEvent.query.filter_by(
                event_type="template_copy", user_id=user_id, source="miniprogram"
            ).count()
            == 1
        )


def test_mp_alerts_warnings_schema_title_type_level_text_times(
    app, client, db_session, monkeypatch
):
    warning = {
        "title": "高温黄色预警",
        "type": "高温",
        "level": "黄色",
        "text": "请注意防暑降温",
        "start_time": "2026-09-05T08:00+08:00",
        "end_time": "2026-09-05T20:00+08:00",
    }
    with app.app_context():
        _user, plain = _create_mp_token(db_session, "mp_alerts_schema")
    _stub_mp_weather(monkeypatch, warnings=[warning])

    created = client.post(
        "/mp/api/v1/elders",
        json={"name": "奶奶", "location_query": "都昌"},
        headers=_auth(plain),
    )
    pair_id = created.get_json()["data"]["pair_id"]

    resp = client.get(f"/mp/api/v1/alerts?pair_id={pair_id}", headers=_auth(plain))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert "data" in body
    assert isinstance(body["data"]["warnings"], list)
    assert len(body["data"]["warnings"]) == 1
    item = body["data"]["warnings"][0]
    for key in ("title", "type", "level", "text", "start_time", "end_time"):
        assert key in item
        assert item[key] == warning[key]
    assert body["data"]["has_official_warning"] is True
    assert "trigger" in body["data"]["weather"]


def test_mp_elders_list_after_create_data_array_chronic_list_pair_id_int(
    app, client, db_session, monkeypatch
):
    with app.app_context():
        _user, plain = _create_mp_token(db_session, "mp_list_after_create")
    _stub_mp_weather(monkeypatch)

    created = client.post(
        "/mp/api/v1/elders",
        json={
            "name": "外婆",
            "relation": "母亲",
            "location_query": "都昌",
            "chronic_diseases": ["高血压", "冠心病"],
        },
        headers=_auth(plain),
    )
    assert created.status_code == 200
    created_body = created.get_json()
    assert created_body["success"] is True
    assert isinstance(created_body["data"]["pair_id"], int)
    assert isinstance(created_body["data"]["member_id"], int)

    resp = client.get("/mp/api/v1/elders", headers=_auth(plain))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 1
    item = body["data"][0]
    assert isinstance(item["pair_id"], int)
    assert item["pair_id"] == created_body["data"]["pair_id"]
    assert item["member"] is not None
    assert isinstance(item["member"]["chronic_diseases"], list)
    assert item["member"]["chronic_diseases"] == ["高血压", "冠心病"]
    assert not isinstance(item["member"]["chronic_diseases"], str)


def test_mp_alerts_weather_available_requires_parsed_tmax_tmin(
    app, client, db_session, monkeypatch
):
    with app.app_context():
        _user, plain = _create_mp_token(db_session, "mp_weather_gate")

    created = client.post(
        "/mp/api/v1/elders",
        json={"name": "爷爷", "location_query": "都昌"},
        headers=_auth(plain),
    )
    pair_id = created.get_json()["data"]["pair_id"]

    missing_extrema = {
        "temperature": 31,
        "temperature_max": None,
        "temperature_min": None,
        "data_source": "QWeather",
        "is_mock": False,
    }
    _stub_mp_weather(monkeypatch, weather=missing_extrema)
    elders = client.get("/mp/api/v1/elders", headers=_auth(plain)).get_json()
    alerts = client.get(f"/mp/api/v1/alerts?pair_id={pair_id}", headers=_auth(plain)).get_json()
    assert elders["data"][0]["today"]["weather_available"] is False
    assert elders["data"][0]["today"]["temperature_max"] is None
    assert alerts["data"]["weather"]["weather_available"] is False
    assert alerts["data"]["weather"]["temperature_max"] is None

    online = {
        "temperature": 31,
        "temperature_max": 36,
        "temperature_min": 26,
        "data_source": "QWeather",
        "is_mock": False,
    }
    _stub_mp_weather(monkeypatch, weather=online)
    elders = client.get("/mp/api/v1/elders", headers=_auth(plain)).get_json()
    alerts = client.get(f"/mp/api/v1/alerts?pair_id={pair_id}", headers=_auth(plain)).get_json()
    assert elders["data"][0]["today"]["weather_available"] is True
    assert elders["data"][0]["today"]["trigger"] == "heat"
    assert alerts["data"]["weather"]["weather_available"] is True
    assert alerts["data"]["weather"]["temperature_max"] == 36.0
    assert alerts["data"]["weather"]["temperature_min"] == 26.0
    assert alerts["data"]["weather"]["trigger"] == "heat"


def test_mp_elders_today_has_official_warning_and_caches_by_location_code(
    app, client, db_session, monkeypatch
):
    with app.app_context():
        _user, plain = _create_mp_token(db_session, "mp_official_cache")

    created_a = client.post(
        "/mp/api/v1/elders",
        json={"name": "对象A", "location_query": "都昌"},
        headers=_auth(plain),
    )
    created_b = client.post(
        "/mp/api/v1/elders",
        json={"name": "对象B", "location_query": "都昌"},
        headers=_auth(plain),
    )
    assert created_a.status_code == 200
    assert created_b.status_code == 200

    online = {
        "temperature": 31,
        "temperature_max": 36,
        "temperature_min": 26,
        "data_source": "QWeather",
        "is_mock": False,
    }
    monkeypatch.setattr(
        "blueprints.mp_api.resolve_location",
        lambda _label: {"location_code": "101240201", "provider": "QWeather"},
    )
    monkeypatch.setattr(
        "blueprints.mp_api.get_weather_with_cache",
        lambda _location: (online, False),
    )
    calls = []

    def fake_warnings(code):
        calls.append(code)
        return [{"title": "高温橙色预警", "type": "高温", "level": "橙色", "text": "x"}]

    monkeypatch.setattr("blueprints.mp_api.get_qweather_warnings", fake_warnings)

    listed = client.get("/mp/api/v1/elders", headers=_auth(plain)).get_json()
    assert listed["success"] is True
    assert len(listed["data"]) == 2
    assert len(calls) == 1
    for item in listed["data"]:
        assert item["today"]["has_official_warning"] is True
        assert item["today"]["trigger"] == "heat"

    monkeypatch.setattr("blueprints.mp_api.get_qweather_warnings", lambda _code: [])
    listed = client.get("/mp/api/v1/elders", headers=_auth(plain)).get_json()
    for item in listed["data"]:
        assert item["today"]["has_official_warning"] is False
        assert item["today"]["trigger"] == "heat"


def test_mp_alerts_missing_pair_id_error_key(app, client, db_session):
    with app.app_context():
        _user, plain = _create_mp_token(db_session, "mp_missing_pair")
    resp = client.get("/mp/api/v1/alerts", headers=_auth(plain))
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"] == "missing_pair_id"


def test_miniprogram_config_declares_api_base_url():
    text = (ROOT / "miniprogram" / "config.js").read_text(encoding="utf-8")
    assert re.search(r"\bAPI_BASE_URL\s*:", text)
    assert "module.exports" in text


def test_miniprogram_request_rejects_empty_api_base_url():
    text = (ROOT / "miniprogram" / "utils" / "request.js").read_text(encoding="utf-8")
    assert "miniapp_api_base_missing" in text
    assert re.search(r"!API_BASE_URL|!base", text)


def test_miniprogram_classify_http_error_codes():
    script = r"""
const { classifyHttp, api } = require('./miniprogram/utils/request');
function expectCode(status, data, code) {
  const result = classifyHttp(status, data);
  if (result.ok) throw new Error('expected failure for ' + status);
  if (result.error.code !== code) throw new Error('got ' + result.error.code);
}
expectCode(401, {}, 'unauthorized');
expectCode(429, '<html>429</html>', 'rate_limited');
expectCode(429, {success: false, error: 'rate_limited'}, 'rate_limited');
expectCode(404, {success: false, error: 'not_found'}, 'request_failed');
const ok = classifyHttp(200, {success: true, data: {a: 1}});
if (!ok.ok || !ok.data || ok.data.a !== 1) throw new Error('ok payload');
const missingData = classifyHttp(200, {success: true});
if (!missingData.ok) throw new Error('success without data should pass');
api({ method: 'GET', path: '/mp/api/v1/me' }).then(() => {
  throw new Error('empty base should reject');
}).catch((err) => {
  if (err.code !== 'miniapp_api_base_missing') throw err;
  console.log('ok');
});
"""
    import subprocess

    proc = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok" in proc.stdout


def test_miniprogram_packaging_files_exist():
    cfg = ROOT / "project.config.json"
    sitemap = ROOT / "miniprogram" / "sitemap.json"
    app_json = ROOT / "miniprogram" / "app.json"
    assert cfg.is_file()
    assert sitemap.is_file()
    import json

    project = json.loads(cfg.read_text(encoding="utf-8"))
    assert project["miniprogramRoot"] == "miniprogram/"
    assert project["compileType"] == "miniprogram"
    assert project["setting"]["es6"] is True
    assert project["setting"]["urlCheck"] is True
    assert project["appid"] == "touristappid"
    app = json.loads(app_json.read_text(encoding="utf-8"))
    assert app["sitemapLocation"] == "sitemap.json"
    config_js = (ROOT / "miniprogram" / "config.js").read_text(encoding="utf-8")
    assert "API_BASE_URL: ''" in config_js or 'API_BASE_URL: ""' in config_js
    assert not (ROOT / "miniprogram" / "project.config.json").exists()
