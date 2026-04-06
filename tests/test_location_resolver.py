# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone


def test_location_resolver_city_map_hit(app, db_session):
    from services.location_resolver import resolve_location
    from core.db_models import LocationCache

    with app.app_context():
        app.config["CITY_LOCATION_MAP"] = {"九江": "116.20,29.27"}
        result = resolve_location("九江")
        assert result["location_code"] == "116.20,29.27"
        assert result["provider"] in ("map", "cache", "raw", "amap")

        cached = LocationCache.query.filter_by(query_text="九江").first()
        assert cached is not None
        assert cached.location_code == "116.20,29.27"


def test_location_resolver_amap_mock_and_cache(app, db_session, monkeypatch):
    from services.location_resolver import resolve_location
    from core.db_models import LocationCache

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "status": "1",
                "geocodes": [
                    {
                        "location": "120.10,30.20",
                        "formatted_address": "测试地址"
                    }
                ]
            }

    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return FakeResp()

    monkeypatch.setattr("services.location_resolver.requests.get", fake_get)

    with app.app_context():
        app.config["AMAP_WEB_SERVICE_KEY"] = "fake-key"
        app.config["CITY_LOCATION_MAP"] = {}

        result1 = resolve_location("杭州市测试地")
        assert result1["location_code"] == "120.10,30.20"
        assert result1["provider"] == "amap"
        assert calls["n"] == 1

        # second call should hit DB cache (no more requests)
        monkeypatch.setattr(
            "services.location_resolver.requests.get",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("should not call")),
        )
        result2 = resolve_location("杭州市测试地")
        assert result2["location_code"] == "120.10,30.20"

        cached = LocationCache.query.filter_by(query_text="杭州市测试地").first()
        assert cached is not None
        assert cached.provider == "amap"

        alias_cached = LocationCache.query.filter_by(query_text="测试地址").first()
        assert alias_cached is not None
        assert alias_cached.location_code == "120.10,30.20"

        result3 = resolve_location("测试地址")
        assert result3["location_code"] == "120.10,30.20"


def test_location_resolver_uses_stale_cache_when_provider_fails(app, db_session, monkeypatch):
    from services.location_resolver import resolve_location
    from core.db_models import LocationCache

    stale_record = LocationCache(
        query_text='老地址',
        location_code='121.00,31.00',
        provider='amap',
        raw_json='{"cached": true}',
        created_at=datetime.now(timezone.utc) - timedelta(days=120),
        updated_at=datetime.now(timezone.utc) - timedelta(days=120),
    )
    db_session.add(stale_record)
    db_session.commit()

    def fake_get(*args, **kwargs):
        raise RuntimeError('network down')

    monkeypatch.setattr("services.location_resolver.requests.get", fake_get)

    with app.app_context():
        app.config["AMAP_WEB_SERVICE_KEY"] = "fake-key"
        app.config["CITY_LOCATION_MAP"] = {}
        app.config["LOCATION_CACHE_TTL_DAYS"] = 30

        result = resolve_location("老地址")
        assert result["location_code"] == "121.00,31.00"
        assert result["provider"] == "stale_cache"
