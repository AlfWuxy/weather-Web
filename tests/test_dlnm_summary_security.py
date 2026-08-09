# -*- coding: utf-8 -*-
"""DLNM 摘要必须认证，并使用固定公开字段合同。"""

import pytest


class _SensitiveSummaryService:
    def get_model_summary(self):
        return {
            "status": "模型已训练",
            "model_source": "profile",
            "profile_loaded": True,
            "profile_name": "public-profile",
            "mmt": 26.5,
            "max_lag": 7,
            "risk_thresholds": {"high": 1.5},
            "profile_path": "/srv/private/model/profile.json",
            "profile_metrics": {"raw_sample_count": 1234},
            "sample_counts": {"private_cohort": 99},
            "secret_field": "future-internal-value",
        }


@pytest.mark.parametrize(
    "path",
    ("/api/v1/dlnm/summary", "/api/dlnm/summary"),
)
def test_dlnm_summary_rejects_anonymous_before_loading_model(
    client,
    monkeypatch,
    path,
):
    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: pytest.fail("匿名请求不得加载模型摘要"),
    )

    response = client.get(path, follow_redirects=False)

    assert response.status_code in (301, 302)
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize(
    "path",
    ("/api/v1/dlnm/summary", "/api/dlnm/summary"),
)
def test_dlnm_summary_rejects_guest_before_loading_model(
    client,
    db_session,
    monkeypatch,
    path,
):
    """游客会话不能绕过正式账号认证读取模型摘要。"""
    assert client.get("/guest", follow_redirects=False).status_code in (301, 302, 303)
    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: pytest.fail("游客请求不得加载模型摘要"),
    )

    response = client.get(path, follow_redirects=False)

    assert response.status_code == 403
    assert response.get_json() == {
        "success": False,
        "error": "real_account_required",
    }


@pytest.mark.parametrize(
    "path",
    ("/api/v1/dlnm/summary", "/api/dlnm/summary"),
)
def test_authenticated_dlnm_summary_uses_explicit_allowlist(
    authenticated_client,
    monkeypatch,
    path,
):
    monkeypatch.setattr(
        "services.dlnm_risk_service.get_dlnm_service",
        lambda: _SensitiveSummaryService(),
    )

    response = authenticated_client.get(path)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["summary"] == {
        "status": "模型已训练",
        "model_source": "profile",
        "profile_loaded": True,
        "profile_name": "public-profile",
        "mmt": 26.5,
        "max_lag": 7,
        "risk_thresholds": {"high": 1.5},
    }
    body = response.get_data(as_text=True)
    assert "profile_path" not in body
    assert "profile_metrics" not in body
    assert "sample_counts" not in body
    assert "secret_field" not in body
    assert "/srv/private" not in body
