# -*- coding: utf-8 -*-
"""预警 severity 去重：location + type + level 窗口内行为护栏。

对应 RT-07 / P2_alert_dedupe：
- 同级（I03）：6h 内第二次调用必须返回同一 WeatherAlert.id
- 升级（I02）：同 location+type 先黄后红 6h 内必须新建两条（不同 level），不复用黄警 id

测试 import 路径（I06，I02/I03 共用）：
    from services.push.dispatch import _get_or_create_weather_alert
在 app + db_session 的 app_context 内直接调用；不必构造 User/Pair/WxPusher。
"""

from datetime import datetime, timedelta, timezone


def test_same_location_type_level_within_6h_reuses_alert_id(app, db_session):
    """同 location+type+level、6h 内第二次 get_or_create 返回同一 alert id。"""
    from core.db_models import WeatherAlert
    from core.time_utils import utcnow
    # I06：唯一允许的 helper import；I02 升级用例同路径。
    from services.push.dispatch import _get_or_create_weather_alert

    with app.app_context():
        now = utcnow()
        location_key = "116.20,29.27"
        alert_type = "高温"
        alert_level = "黄色"

        first = _get_or_create_weather_alert(
            now=now,
            location_key=location_key,
            alert_type=alert_type,
            alert_level=alert_level,
            description="高温黄色预警（首次）",
            dedupe_hours=6,
        )
        db_session.commit()
        first_id = first.id

        assert first_id is not None
        assert WeatherAlert.query.count() == 1
        assert first.location == location_key
        assert first.alert_type == alert_type
        assert first.alert_level == alert_level

        # 同窗内再次调用：文案可变，键不变 → 必须复用同一 id
        second = _get_or_create_weather_alert(
            now=now,
            location_key=location_key,
            alert_type=alert_type,
            alert_level=alert_level,
            description="高温黄色预警（再次，文案不同）",
            dedupe_hours=6,
        )
        db_session.commit()

        assert second.id == first_id
        assert WeatherAlert.query.count() == 1
        # 命中 recent 时不改写既有行（当前实现直接 return）
        refreshed = WeatherAlert.query.filter_by(id=first_id).first()
        assert refreshed is not None
        assert refreshed.description == "高温黄色预警（首次）"


def test_yellow_then_red_within_6h_creates_two_alerts_not_reusing_id(app, db_session):
    """同 location+type 先黄后红、6h 内必须新建两条 WeatherAlert（不同 level），不复用黄警 id。"""
    from core.db_models import WeatherAlert
    from core.time_utils import utcnow
    from services.push.dispatch import _get_or_create_weather_alert

    with app.app_context():
        now = utcnow()
        location_key = "116.20,29.27"
        alert_type = "高温"

        yellow = _get_or_create_weather_alert(
            now=now,
            location_key=location_key,
            alert_type=alert_type,
            alert_level="黄色",
            description="高温黄色预警",
            dedupe_hours=6,
        )
        db_session.commit()
        yellow_id = yellow.id

        assert yellow_id is not None
        assert WeatherAlert.query.count() == 1
        assert yellow.alert_level == "黄色"

        # 同窗、同地点同类型、级别升级为红色 → 必须新建，禁止复用黄警 id
        red = _get_or_create_weather_alert(
            now=now,
            location_key=location_key,
            alert_type=alert_type,
            alert_level="红色",
            description="高温红色预警",
            dedupe_hours=6,
        )
        db_session.commit()
        red_id = red.id

        assert red_id is not None
        assert red_id != yellow_id
        assert WeatherAlert.query.count() == 2
        assert red.alert_level == "红色"
        assert red.location == location_key
        assert red.alert_type == alert_type

        # 黄警行保持独立，未被改写为红
        yellow_row = WeatherAlert.query.filter_by(id=yellow_id).first()
        red_row = WeatherAlert.query.filter_by(id=red_id).first()
        assert yellow_row is not None and red_row is not None
        assert yellow_row.alert_level == "黄色"
        assert red_row.alert_level == "红色"
        assert {yellow_row.alert_level, red_row.alert_level} == {"黄色", "红色"}


def test_source_and_official_status_are_dedupe_dimensions(app, db_session):
    """相同地点、类型、级别仍须按来源和官方性拆成独立记录。"""
    from core.db_models import WeatherAlert
    from services.push.dispatch import _get_or_create_weather_alert

    with app.app_context():
        now = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
        shared = {
            "now": now,
            "location_key": "116.20,29.27",
            "alert_type": "高温",
            "alert_level": "黄色",
            "description": "同一展示文案",
            "dedupe_hours": 6,
        }

        qweather = _get_or_create_weather_alert(
            **shared,
            source="QWeather",
            is_official=True,
        )
        same_source_threshold = _get_or_create_weather_alert(
            **shared,
            source="QWeather",
            is_official=False,
        )
        app_threshold = _get_or_create_weather_alert(
            **shared,
            source="AppThreshold",
            is_official=False,
        )
        db_session.commit()

        assert WeatherAlert.query.count() == 3
        assert len({qweather.id, same_source_threshold.id, app_threshold.id}) == 3
        assert qweather.source == "QWeather" and qweather.is_official is True
        assert app_threshold.source == "AppThreshold" and app_threshold.is_official is False


def test_reused_official_alert_accepts_richer_validity_and_description(app, db_session):
    """同一官方预警复用 id，并允许新鲜非空字段补全，空值与短文案不得降级。"""
    from core.db_models import WeatherAlert
    from core.time_utils import ensure_utc_aware
    from services.push.dispatch import _get_or_create_weather_alert

    with app.app_context():
        now = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
        shared = {
            "location_key": "116.20,29.27",
            "alert_type": "高温",
            "alert_level": "黄色",
            "dedupe_hours": 6,
            "source": "QWeather",
            "is_official": True,
        }

        first = _get_or_create_weather_alert(
            now=now,
            description="官方预警",
            **shared,
        )
        db_session.commit()
        first_id = first.id

        starts_at = now - timedelta(hours=2)
        ends_at = now + timedelta(hours=10)
        second = _get_or_create_weather_alert(
            now=now + timedelta(minutes=10),
            description="预计白天最高气温将超过 35°C，请注意防暑降温。",
            starts_at=starts_at,
            ends_at=ends_at,
            **shared,
        )
        db_session.commit()

        assert second.id == first_id
        assert WeatherAlert.query.count() == 1
        assert second.description == "预计白天最高气温将超过 35°C，请注意防暑降温。"
        assert ensure_utc_aware(second.starts_at) == starts_at
        assert ensure_utc_aware(second.ends_at) == ends_at

        third = _get_or_create_weather_alert(
            now=now + timedelta(minutes=20),
            description="高温预警",
            starts_at=None,
            ends_at=None,
            **shared,
        )
        db_session.commit()

        assert third.id == first_id
        assert third.description == second.description
        assert ensure_utc_aware(third.starts_at) == starts_at
        assert ensure_utc_aware(third.ends_at) == ends_at
