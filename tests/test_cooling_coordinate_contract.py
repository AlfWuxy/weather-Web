# -*- coding: utf-8 -*-
"""避暑资源坐标来源、核验回执与公开输出测试。"""

from datetime import datetime, timezone
import math

import pytest

from blueprints.admin import _parse_cooling_coordinates


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        ("29.270001", "116.200001"),
        (29.27, 116.20),
    ],
)
def test_parse_cooling_coordinates_accepts_valid_gcj02_pair(latitude, longitude):
    assert _parse_cooling_coordinates(latitude, longitude) == (
        float(latitude),
        float(longitude),
    )


def test_parse_cooling_coordinates_accepts_empty_pair():
    assert _parse_cooling_coordinates("", None) == (None, None)


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        ("29.27", ""),
        ("", "116.20"),
        ("91", "116.20"),
        ("29.27", "181"),
        ("nan", "116.20"),
        ("29.27", "inf"),
        ("116.20", "29.27"),
        ("28.40", "115.86"),
        ("29.27", "115.00"),
    ],
)
def test_parse_cooling_coordinates_rejects_incomplete_or_invalid_pair(latitude, longitude):
    with pytest.raises(ValueError):
        _parse_cooling_coordinates(latitude, longitude)


def test_public_cooling_resource_declares_gcj02_contract(client, db_session):
    from core.db_models import CoolingResource

    db_session.add(
        CoolingResource(
            community_code="测试社区",
            name="已核验纳凉点",
            latitude=29.27,
            longitude=116.20,
            coordinate_system="GCJ-02",
            coordinate_source="管理员现场使用微信地图人工核对",
            coordinate_verified_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
            is_active=True,
        )
    )
    db_session.commit()

    response = client.get("/mp/api/v1/public/cooling-resources")
    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["coordinate_system"] == "GCJ-02"
    assert payload["items"][0]["coordinate_system"] == "GCJ-02"
    assert math.isfinite(payload["items"][0]["latitude"])
    assert math.isfinite(payload["items"][0]["longitude"])
    assert "coordinate_source" not in payload["items"][0]


@pytest.mark.parametrize(
    ("coordinate_system", "coordinate_verified_at", "coordinate_source"),
    [
        (None, None, "内部核对记录"),
        ("GCJ-02", None, "内部核对记录"),
        ("WGS84", datetime(2026, 7, 21, tzinfo=timezone.utc), "内部核对记录"),
        ("GCJ-02", datetime(2026, 7, 21, tzinfo=timezone.utc), None),
    ],
)
def test_public_resource_keeps_text_but_hides_unverified_coordinates(
    client,
    db_session,
    coordinate_system,
    coordinate_verified_at,
    coordinate_source,
):
    from core.db_models import CoolingResource

    db_session.add(
        CoolingResource(
            community_code="测试社区",
            name="仅公开文字资料",
            address_hint="社区服务中心一楼",
            latitude=29.27,
            longitude=116.20,
            coordinate_system=coordinate_system,
            coordinate_source=coordinate_source,
            coordinate_verified_at=coordinate_verified_at,
            is_active=True,
        )
    )
    db_session.commit()

    item = client.get(
        "/mp/api/v1/public/cooling-resources"
    ).get_json()["data"]["items"][0]
    assert item["name"] == "仅公开文字资料"
    assert item["address_hint"] == "社区服务中心一楼"
    assert item["latitude"] is None
    assert item["longitude"] is None
    assert item["coordinate_system"] is None
    assert "coordinate_source" not in item


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (999.0, 116.20),
        (29.27, 999.0),
        (28.40, 115.86),
    ],
)
def test_public_resource_hides_invalid_or_out_of_area_verified_coordinates(
    client,
    db_session,
    latitude,
    longitude,
):
    """异常导入的数据即使带核验字段，也不能进入距离排序或地图。"""
    from core.db_models import CoolingResource

    db_session.add(
        CoolingResource(
            community_code="测试社区",
            name="异常坐标点位",
            address_hint="仅保留文字地址",
            latitude=latitude,
            longitude=longitude,
            coordinate_system="GCJ-02",
            coordinate_source="异常导入记录",
            coordinate_verified_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
            is_active=True,
        )
    )
    db_session.commit()

    item = client.get(
        "/mp/api/v1/public/cooling-resources"
    ).get_json()["data"]["items"][0]
    assert item["address_hint"] == "仅保留文字地址"
    assert item["latitude"] is None
    assert item["longitude"] is None
    assert item["coordinate_system"] is None


def test_admin_create_requires_provenance_before_coordinate_can_be_verified(
    admin_client,
    db_session,
):
    from core.db_models import CoolingResource

    response = admin_client.post(
        "/admin/cooling/add",
        data={
            "csrf_token": "test-csrf-token",
            "community_code": "测试社区",
            "name": "来源缺失点位",
            "latitude": "29.27",
            "longitude": "116.20",
            "coordinate_system": "GCJ-02",
            "coordinate_verified": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    assert CoolingResource.query.filter_by(name="来源缺失点位").count() == 0


def test_admin_can_create_verified_gcj02_resource(admin_client, db_session):
    from core.db_models import CoolingResource

    response = admin_client.post(
        "/admin/cooling/add",
        data={
            "csrf_token": "test-csrf-token",
            "community_code": "测试社区",
            "name": "新核验点位",
            "latitude": "29.27",
            "longitude": "116.20",
            "coordinate_system": "GCJ-02",
            "coordinate_source": "管理员现场使用微信地图核对，2026-07-21",
            "coordinate_verified": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    resource = CoolingResource.query.filter_by(name="新核验点位").one()
    assert resource.coordinate_system == "GCJ-02"
    assert resource.coordinate_source.startswith("管理员现场")
    assert resource.coordinate_verified_at is not None
    item = admin_client.get(
        "/mp/api/v1/public/cooling-resources"
    ).get_json()["data"]["items"][0]
    assert item["latitude"] == pytest.approx(29.27)
    assert item["longitude"] == pytest.approx(116.20)
    assert item["coordinate_system"] == "GCJ-02"


def test_admin_can_save_coordinates_as_unverified(admin_client, db_session):
    from core.db_models import CoolingResource

    response = admin_client.post(
        "/admin/cooling/add",
        data={
            "csrf_token": "test-csrf-token",
            "community_code": "测试社区",
            "name": "待复核点位",
            "latitude": "29.27",
            "longitude": "116.20",
            "coordinate_system": "GCJ-02",
            "coordinate_source": "社区电话提供，等待现场复核",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    resource = CoolingResource.query.filter_by(name="待复核点位").one()
    assert resource.coordinate_verified_at is None
    item = admin_client.get(
        "/mp/api/v1/public/cooling-resources"
    ).get_json()["data"]["items"][0]
    assert item["latitude"] is None
    assert item["longitude"] is None
    assert item["coordinate_system"] is None


def test_admin_coordinate_change_without_recheck_clears_verification(
    admin_client,
    db_session,
):
    from core.db_models import CoolingResource

    resource = CoolingResource(
        community_code="测试社区",
        name="待重新核验点位",
        latitude=29.27,
        longitude=116.20,
        coordinate_system="GCJ-02",
        coordinate_source="旧来源",
        coordinate_verified_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        is_active=True,
    )
    db_session.add(resource)
    db_session.commit()

    response = admin_client.post(
        f"/admin/cooling/{resource.id}/edit",
        data={
            "csrf_token": "test-csrf-token",
            "community_code": "测试社区",
            "name": "待重新核验点位",
            "latitude": "29.28",
            "longitude": "116.21",
            "coordinate_system": "GCJ-02",
            "coordinate_source": "新来源",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    db_session.refresh(resource)
    assert resource.coordinate_source == "新来源"
    assert resource.coordinate_verified_at is None
    item = admin_client.get(
        "/mp/api/v1/public/cooling-resources"
    ).get_json()["data"]["items"][0]
    assert item["latitude"] is None
    assert item["coordinate_system"] is None


def test_admin_recheck_after_source_change_records_new_verification(
    admin_client,
    db_session,
):
    from core.db_models import CoolingResource

    old_verified_at = datetime(2026, 7, 20)
    resource = CoolingResource(
        community_code="测试社区",
        name="重新核验完成点位",
        latitude=29.27,
        longitude=116.20,
        coordinate_system="GCJ-02",
        coordinate_source="旧来源",
        coordinate_verified_at=old_verified_at,
        is_active=True,
    )
    db_session.add(resource)
    db_session.commit()

    response = admin_client.post(
        f"/admin/cooling/{resource.id}/edit",
        data={
            "csrf_token": "test-csrf-token",
            "community_code": "测试社区",
            "name": "重新核验完成点位",
            "latitude": "29.27",
            "longitude": "116.20",
            "coordinate_system": "GCJ-02",
            "coordinate_source": "管理员现场复核的新来源",
            "coordinate_verified": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    db_session.refresh(resource)
    assert resource.coordinate_verified_at is not None
    assert resource.coordinate_verified_at != old_verified_at


def test_clearing_coordinates_clears_all_verification_fields(admin_client, db_session):
    from core.db_models import CoolingResource

    resource = CoolingResource(
        community_code="测试社区",
        name="清空坐标点位",
        latitude=29.27,
        longitude=116.20,
        coordinate_system="GCJ-02",
        coordinate_source="旧来源",
        coordinate_verified_at=datetime(2026, 7, 20),
        is_active=True,
    )
    db_session.add(resource)
    db_session.commit()

    response = admin_client.post(
        f"/admin/cooling/{resource.id}/edit",
        data={
            "csrf_token": "test-csrf-token",
            "community_code": "测试社区",
            "name": "清空坐标点位",
            "latitude": "",
            "longitude": "",
            "coordinate_system": "GCJ-02",
            "coordinate_source": "这项应被同步清除",
            "coordinate_verified": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    db_session.refresh(resource)
    assert resource.latitude is None
    assert resource.longitude is None
    assert resource.coordinate_system is None
    assert resource.coordinate_source is None
    assert resource.coordinate_verified_at is None


def test_admin_can_create_disabled_resource_without_checkbox(admin_client, db_session):
    from core.db_models import CoolingResource

    response = admin_client.post(
        "/admin/cooling/add",
        data={
            "csrf_token": "test-csrf-token",
            "community_code": "测试社区",
            "name": "暂未开放点位",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    resource = CoolingResource.query.filter_by(name="暂未开放点位").one()
    assert resource.is_active is False
    public = admin_client.get("/mp/api/v1/public/cooling-resources").get_json()["data"]
    assert public["items"] == []


def test_admin_can_disable_existing_resource_without_checkbox(admin_client, db_session):
    from core.db_models import CoolingResource

    resource = CoolingResource(
        community_code="测试社区",
        name="需要下线点位",
        is_active=True,
    )
    db_session.add(resource)
    db_session.commit()

    response = admin_client.post(
        f"/admin/cooling/{resource.id}/edit",
        data={
            "csrf_token": "test-csrf-token",
            "community_code": "测试社区",
            "name": "需要下线点位",
        },
        follow_redirects=False,
    )

    assert response.status_code in (301, 302, 303)
    db_session.refresh(resource)
    assert resource.is_active is False
    public = admin_client.get("/mp/api/v1/public/cooling-resources").get_json()["data"]
    assert public["items"] == []
