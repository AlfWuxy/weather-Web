# -*- coding: utf-8 -*-
"""网页公开风险页与小程序 bootstrap 的同源回归测试。"""

import copy
from datetime import datetime, timezone
from html import unescape
import json

import pytest


CURRENT = {
    "temperature": 36.0,
    "temperature_max": 38.0,
    "temperature_min": 28.0,
    "humidity": 72.0,
    "weather_condition": "晴",
    "consecutive_hot_days": 3,
    "is_mock": False,
    "data_source": "QWeather",
}
WARNINGS = [
    {
        "title": "高温橙色预警",
        "type": "高温",
        "text": "午后减少户外活动。",
    }
]
FIXED_NOW = datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)


def _action_snapshot():
    return {
        "snapshot_id": "snapshot-contract-001",
        "available": True,
        "stale": False,
        "current": dict(CURRENT),
        "risk": {
            "available": True,
            "level": "高风险",
            "score": 80,
            "calculation": {
                "heat_result": {"risk_score": 80},
                "risk_reasons": ["高温测试原因"],
            },
        },
        "actions": [{"id": "drink_water", "title": "喝水"}],
    }


def test_snapshot_component_status_keeps_only_precise_independent_warning():
    from services.miniprogram_service import snapshot_component_status

    precise_warning = {
        "available": False,
        "stale": True,
        "warnings_stale": False,
        "source_status": {
            "warnings": {"available": True, "stale": False},
        },
    }
    incomplete_warning = {
        "available": False,
        "stale": False,
        "source_status": {"warnings": {"available": True}},
    }
    contradictory_current = {
        "available": False,
        "stale": True,
        "current_stale": False,
        "source_status": {
            "current": {"available": True, "stale": False},
        },
    }
    malformed_current = {
        "available": True,
        "stale": False,
        "current_stale": False,
        "source_status": {
            "current": {"available": "false", "stale": False},
        },
    }
    malformed_warning_stale = {
        "available": False,
        "stale": True,
        "warnings_stale": False,
        "source_status": {
            "warnings": {"available": True, "stale": "false"},
        },
    }

    assert snapshot_component_status(precise_warning, "warnings")["usable"] is True
    assert snapshot_component_status(incomplete_warning, "warnings")["usable"] is False
    assert snapshot_component_status(contradictory_current, "current")["usable"] is False
    assert snapshot_component_status(malformed_current, "current")["usable"] is False
    assert snapshot_component_status(malformed_warning_stale, "warnings")["usable"] is False


def test_public_risk_page_rejects_root_unavailable_contradictory_risk(
    client,
    monkeypatch,
):
    from services import public_service

    payload = _action_snapshot()
    payload.update({
        'available': False,
        'risk_stale': False,
        'source_status': {
            'risk': {'available': True, 'stale': False},
        },
    })
    monkeypatch.setattr(public_service, 'get_bootstrap_payload', lambda: payload)

    body = client.get('/risk').get_data(as_text=True)

    assert '公开天气快照暂未就绪' in body
    assert '当前风险：高风险' not in body


def _forbid_weather_requests(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("公开风险读取不得触发天气或预警请求")

    monkeypatch.setattr(
        "services.public_service.get_weather_with_cache",
        forbidden,
    )
    monkeypatch.setattr(
        "services.public_service.get_consecutive_hot_days",
        forbidden,
    )
    monkeypatch.setattr("requests.get", forbidden)


def test_web_and_bootstrap_share_persisted_risk_actions_and_reminder(
    app,
    client,
    db_session,
    monkeypatch,
):
    from services.miniprogram_service import persist_snapshot

    monkeypatch.setattr(
        "services.miniprogram_service.utcnow",
        lambda: FIXED_NOW,
    )
    _forbid_weather_requests(monkeypatch)
    with app.app_context():
        persist_snapshot(
            CURRENT,
            [],
            WARNINGS,
            fetched_at=FIXED_NOW,
        )

    bootstrap_response = client.get("/mp/api/v1/bootstrap")
    web_response = client.get("/risk?location=任意地点")

    assert bootstrap_response.status_code == 200
    assert web_response.status_code == 200
    bootstrap = bootstrap_response.get_json()["data"]
    body = unescape(web_response.get_data(as_text=True))
    risk = bootstrap["risk"]
    reminder = bootstrap["family_reminder"]

    assert risk["score"] is not None
    assert f"当前风险：{risk['level']}" in body
    assert f"综合评分 {risk['score']}" in body
    for action in bootstrap["actions"]:
        assert action["title"] in body
        assert action["detail"] in body
    assert reminder["message"] in body
    assert reminder["follow_up_question"] in body
    assert f'datetime="{reminder["date"]}"' in body
    assert "都昌县" in body
    assert "地区：任意地点" not in body
    assert 'id="copyFamilyReminder"' in body
    assert 'aria-label="复制今日提醒正文和追问"' in body
    assert 'aria-describedby="familyReminderCopyStatus"' in body
    assert 'role="status"' in body
    assert "/static/js/risk-reminder.js" in body


def test_unavailable_persisted_weather_degrades_both_surfaces_safely(
    app,
    client,
    db_session,
    monkeypatch,
):
    from services.miniprogram_service import persist_snapshot

    monkeypatch.setattr(
        "services.miniprogram_service.utcnow",
        lambda: FIXED_NOW,
    )
    _forbid_weather_requests(monkeypatch)
    with app.app_context():
        persist_snapshot({}, [], [], fetched_at=FIXED_NOW)

    bootstrap_response = client.get("/mp/api/v1/bootstrap")
    web_response = client.get("/risk")

    assert bootstrap_response.status_code == 200
    assert web_response.status_code == 200
    bootstrap = bootstrap_response.get_json()["data"]
    body = unescape(web_response.get_data(as_text=True))
    reminder = bootstrap["family_reminder"]

    assert bootstrap["available"] is False
    assert bootstrap["risk"]["level"] == "未知"
    assert bootstrap["risk"]["score"] is None
    assert bootstrap["actions"] == []
    assert "天气更新中" in body
    assert "当前风险：" not in body
    assert reminder["message"] in body
    assert reminder["follow_up_question"] in body
    assert reminder["date"] in body
    assert 'id="copyFamilyReminder"' in body


def test_partial_weather_fails_closed_for_web_and_miniprogram(
    app,
    client,
    db_session,
    monkeypatch,
):
    from services.miniprogram_service import persist_snapshot

    partial_weather = {
        "temperature": 36.0,
        "temperature_max": 38.0,
        "temperature_min": None,
        "humidity": 72.0,
        "data_source": "QWeather",
        "is_mock": False,
    }
    monkeypatch.setattr("services.miniprogram_service.utcnow", lambda: FIXED_NOW)
    _forbid_weather_requests(monkeypatch)
    with app.app_context():
        persist_snapshot(partial_weather, [], [], fetched_at=FIXED_NOW)

    bootstrap_response = client.get("/mp/api/v1/bootstrap")
    web_response = client.get("/risk")

    assert bootstrap_response.status_code == 200
    assert web_response.status_code == 200
    bootstrap = bootstrap_response.get_json()["data"]
    body = unescape(web_response.get_data(as_text=True))
    assert bootstrap["available"] is True
    assert bootstrap["risk"]["available"] is False
    assert bootstrap["risk"]["level"] == "未知"
    assert bootstrap["actions"] == []
    assert "天气更新中" in body
    assert "当前风险：" not in body


def test_fresh_snapshot_keeps_persisted_risk_and_actions(
    app,
    client,
    db_session,
    monkeypatch,
):
    from core.db_models import MiniProgramSnapshot
    from services.miniprogram_service import persist_snapshot

    monkeypatch.setattr("services.miniprogram_service.utcnow", lambda: FIXED_NOW)
    with app.app_context():
        record = persist_snapshot(CURRENT, [], WARNINGS, fetched_at=FIXED_NOW)
        expected_risk = json.loads(record.risk_json)
        expected_risk.update({
            "level": "高风险",
            "score": 67.0,
            "summary": "已落库的稳定风险",
            "reasons": ["已落库的稳定风险"],
        })
        expected_risk["calculation"]["heat_result"]["risk_level"] = "high"
        expected_risk["calculation"]["heat_result"]["risk_score"] = 67.0
        expected_risk["calculation"]["risk_reasons"] = [
            {"label": "已落库因子", "value": "稳定值", "weight": 100},
        ]
        expected_actions = [{"id": "stable", "title": "稳定行动", "detail": "读取快照"}]
        record.risk_json = json.dumps(expected_risk, ensure_ascii=False)
        record.actions_json = json.dumps(expected_actions, ensure_ascii=False)
        db_session.commit()
        snapshot_id = record.snapshot_id

    monkeypatch.setattr(
        "services.miniprogram_service.calculate_public_risk",
        lambda *_args, **_kwargs: pytest.fail("读取同一快照不得重新计算风险"),
    )
    response = client.get("/mp/api/v1/bootstrap")
    web_response = client.get("/risk")

    assert response.status_code == 200
    assert web_response.status_code == 200
    payload = response.get_json()["data"]
    body = unescape(web_response.get_data(as_text=True))
    assert payload["snapshot_id"] == snapshot_id
    assert payload["risk"] == expected_risk
    assert payload["actions"] == expected_actions
    assert "综合评分 67.0" in body
    assert "已落库因子" in body
    assert "稳定值" in body


def test_action_page_context_consumes_same_snapshot_without_weather_request(
    app,
    db_session,
    monkeypatch,
):
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import today_local
    from services.miniprogram_service import get_bootstrap_payload, persist_snapshot
    from services.public_service import _build_action_context

    monkeypatch.setattr("services.miniprogram_service.utcnow", lambda: FIXED_NOW)
    _forbid_weather_requests(monkeypatch)
    with app.app_context():
        user = User(username="snapshot_action_user", role="user")
        user.set_password("snapshot-action-pass")
        db_session.add(user)
        db_session.flush()
        pair = Pair(
            caregiver_id=user.id,
            community_code="都昌县",
            location_query="任意输入地点",
            elder_code="snapshot-action-elder",
            short_code="73173173",
            short_code_hash=hash_short_code("73173173"),
            status="active",
            created_at=FIXED_NOW,
            last_active_at=FIXED_NOW,
        )
        db_session.add(pair)
        persist_snapshot(CURRENT, [], WARNINGS, fetched_at=FIXED_NOW)
        snapshot = get_bootstrap_payload(now=FIXED_NOW)

        status, actions, _resources, weather, heat_result, risk_label, reasons = (
            _build_action_context(pair, today_local())
        )

    assert status.risk_level == snapshot["risk"]["level"]
    assert risk_label == snapshot["risk"]["level"]
    assert heat_result["risk_score"] == snapshot["risk"]["score"]
    assert actions == snapshot["actions"]
    assert weather == snapshot["current"]
    assert reasons


@pytest.mark.parametrize(
    "broken_contract",
    (
        "snapshot_id",
        "available",
        "stale",
        "current_source",
        "risk_available",
        "risk_level",
    ),
)
def test_web_and_miniprogram_never_persist_risk_from_invalid_snapshot_contract(
    app,
    db_session,
    monkeypatch,
    broken_contract,
):
    """行动写路径只接受完整、新鲜、真实且声明可用的同一县级快照。"""
    from blueprints import mp_api
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import today_local
    from services import public_service

    snapshot = copy.deepcopy(_action_snapshot())
    if broken_contract == "snapshot_id":
        snapshot["snapshot_id"] = None
    elif broken_contract == "available":
        snapshot["available"] = False
    elif broken_contract == "stale":
        snapshot["stale"] = True
    elif broken_contract == "current_source":
        snapshot["current"]["data_source"] = "Demo"
    elif broken_contract == "risk_available":
        snapshot["risk"]["available"] = False
    elif broken_contract == "risk_level":
        snapshot["risk"]["level"] = "未知"

    monkeypatch.setattr(public_service, "get_bootstrap_payload", lambda: snapshot)
    monkeypatch.setattr(mp_api, "get_bootstrap_payload", lambda: snapshot)
    owner = User(username=f"invalid-snapshot-{broken_contract}", role="caregiver")
    owner.set_password("invalid-snapshot-password")
    db_session.add(owner)
    db_session.flush()
    pair = Pair(
        caregiver_id=owner.id,
        community_code="都昌县",
        location_query="都昌县",
        elder_code=f"invalid-snapshot-{broken_contract}",
        short_code="74217421",
        short_code_hash=hash_short_code("74217421"),
        status="active",
        created_at=FIXED_NOW,
        last_active_at=FIXED_NOW,
    )
    db_session.add(pair)
    db_session.flush()

    web_context = public_service._build_action_context(pair, today_local())
    mini_status = mp_api._daily_status_for_pair(pair)

    assert web_context[0].risk_level is None
    assert web_context[1] == []
    assert web_context[3] is None
    assert mini_status.risk_level is None


def test_web_and_miniprogram_persist_the_same_valid_snapshot_risk(
    app,
    db_session,
    monkeypatch,
):
    """完整县级快照在两个行动入口只落一次相同风险等级。"""
    from blueprints import mp_api
    from core.db_models import DailyStatus, Pair, User
    from core.security import hash_short_code
    from core.time_utils import today_local
    from services import public_service

    snapshot = _action_snapshot()
    monkeypatch.setattr(public_service, "get_bootstrap_payload", lambda: snapshot)
    monkeypatch.setattr(mp_api, "get_bootstrap_payload", lambda: snapshot)
    owner = User(username="valid-shared-snapshot", role="caregiver")
    owner.set_password("valid-snapshot-password")
    db_session.add(owner)
    db_session.flush()
    pair = Pair(
        caregiver_id=owner.id,
        community_code="都昌县",
        location_query="都昌县",
        elder_code="valid-shared-snapshot",
        short_code="75317531",
        short_code_hash=hash_short_code("75317531"),
        status="active",
        created_at=FIXED_NOW,
        last_active_at=FIXED_NOW,
    )
    db_session.add(pair)
    db_session.flush()

    mini_status = mp_api._daily_status_for_pair(pair)
    web_context = public_service._build_action_context(pair, today_local())
    db_session.flush()

    assert mini_status.id == web_context[0].id
    assert mini_status.risk_level == "高风险"
    assert web_context[5] == "高风险"
    assert DailyStatus.query.filter_by(
        pair_id=pair.id,
        status_date=today_local(),
    ).count() == 1


def test_forecast_only_stale_keeps_web_and_miniprogram_risk_actions(
    app,
    db_session,
    monkeypatch,
):
    from blueprints import mp_api
    from core.db_models import Pair, User
    from core.security import hash_short_code
    from core.time_utils import today_local
    from services import public_service

    snapshot = _action_snapshot()
    snapshot.update({
        'stale': True,
        'forecast_stale': True,
        'current_stale': False,
        'risk_stale': False,
        'source_status': {
            'current': {'available': True, 'stale': False},
            'risk': {'available': True, 'stale': False},
            'forecast': {'available': True, 'stale': True},
        },
    })
    monkeypatch.setattr(public_service, 'get_bootstrap_payload', lambda: snapshot)
    monkeypatch.setattr(mp_api, 'get_bootstrap_payload', lambda: snapshot)
    owner = User(username='forecast-stale-shared', role='caregiver')
    owner.set_password('forecast-stale-password')
    db_session.add(owner)
    db_session.flush()
    pair = Pair(
        caregiver_id=owner.id,
        community_code='都昌县',
        location_query='都昌县',
        elder_code='forecast-stale-shared',
        short_code='76427642',
        short_code_hash=hash_short_code('76427642'),
        status='active',
        created_at=FIXED_NOW,
        last_active_at=FIXED_NOW,
    )
    db_session.add(pair)
    db_session.flush()

    web_context = public_service._build_action_context(pair, today_local())
    mini_status = mp_api._daily_status_for_pair(pair)

    assert web_context[1] == snapshot['actions']
    assert web_context[5] == '高风险'
    assert mini_status.risk_level == '高风险'
