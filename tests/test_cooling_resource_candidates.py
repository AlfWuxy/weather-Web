# -*- coding: utf-8 -*-
"""高德避暑资源候选库的安全发布边界。"""

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = PROJECT_ROOT / "data/cooling_resource_candidates.json"


def _load_candidates():
    return json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))


def test_amap_candidate_preview_is_recorded_but_never_auto_published():
    payload = _load_candidates()
    items = payload["items"]

    assert payload["publication_status"] == "candidate_only"
    assert payload["coordinate_system"] == "GCJ-02"
    assert len(items) >= 10
    assert len({item["source_id"] for item in items}) == len(items)
    assert all(item["verification_status"] == "pending_human_verification" for item in items)
    assert all(item["is_active"] is False for item in items)
    assert all(115.7 <= item["longitude"] <= 116.8 for item in items)
    assert all(28.8 <= item["latitude"] <= 29.8 for item in items)


def test_medical_candidates_are_not_labeled_as_public_cooling_sites():
    items = _load_candidates()["items"]
    medical = [
        item for item in items
        if item["category"] in {"hospital", "health_center"}
    ]

    assert medical
    assert all(item["public_role"] == "medical_support" for item in medical)
    assert all(item["public_role"] != "cooling_candidate" for item in medical)


def test_candidate_preview_requires_login(client, db_session):
    response = client.get("/admin/cooling/candidates", follow_redirects=False)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_candidate_preview_rejects_ordinary_user(authenticated_client):
    response = authenticated_client.get(
        "/admin/cooling/candidates",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


def test_candidate_preview_is_read_only_for_admin(admin_client):
    response = admin_client.get("/admin/cooling/candidates")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "高德资源候选预览" in html
    assert "这些点位尚未公开" in html
    assert "都昌县图书馆" in html
    assert "医疗支持" in html
    assert "待人工核验" in html
    assert "一键启用" not in html


def test_candidate_can_prefill_manual_form_without_auto_verification(admin_client):
    response = admin_client.get(
        "/admin/cooling/add?candidate=B03180SL06"
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'value="都昌县图书馆"' in html
    assert 'value="116.187665"' in html
    assert 'value="29.249263"' in html
    assert "高德 Place Text API v5 候选 B03180SL06" in html
    assert 'id="coordinateVerified" checked' not in html
    assert 'id="isActive" checked' not in html
