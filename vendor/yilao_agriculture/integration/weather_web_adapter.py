"""天气通接入用的纯JSON白名单适配器；不联网、不读取密钥、不改原站。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import math
import re
from zoneinfo import ZoneInfo

from yilao_agri.audit import verify_plan
from yilao_agri.community_catalog import get_community_catalog
from yilao_agri.community_service import build_request, readiness
from yilao_agri.community_store import json_text, validate_state
from yilao_agri.models import request_digest, alert_query_issues
from yilao_agri.recalc import check_plan
from yilao_agri.forecast_run import validate_run_snapshot, validate_run_location

SCHEMA_VERSION = "yilao-weather-web-display-1"
WARNING_SCHEMA = "yilao-warning-provenance-1"
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
STATUS_LABELS = {"blocked": "资料或核查未通过，暂不展示安排", "expired": "安排已过期，请重新核对",
                 "demonstration": "虚构演示安排", "draft": "按已确认资料生成的安排草稿"}
FAILURE_STATES = {"not_queried", "not_configured", "auth_error", "quota_skipped", "network_error",
                  "http_error", "provider_error", "parse_error", "incomplete", "legacy_cache"}
TASK_ALIASES = {"weeding": "weed_hand", "carry_material": "haul_material", "manure_carry": "haul_material",
                "fertilization": "topdress", "apply_material": "topdress", "land_prep": "tillage"}
GENERIC_TASK_LABELS = {"fertilization": "施肥", "apply_material": "施肥", "carry_material": "农资搬运"}


def _time(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("时间缺失")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("时间缺少时区")
    return result.astimezone(timezone.utc)


def _finite(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("非有限数值")
    if value < 0 or (positive and value == 0):
        raise ValueError("数值超出范围")
    return value


def _unknown(code: str, *, alert_count: int = 0) -> dict:
    return {"state": "unknown", "coverage_status": "query_incomplete", "blocks_real_plan": True,
            "alert_count": alert_count, "reason_codes": [code], "queried_at": None, "issued_at": None,
            "meaning": "预警查询未完整核实；空列表不表示当地没有预警"}


def _assess_warning_envelope(envelope, *, expected_spatial_id: str,
                             now: str | datetime | None = None,
                             max_age_minutes: float) -> dict:
    """检查拟议官方预警来源信封；旧列表和DLNM综合色级都只得到unknown。"""
    moment = _time(now or datetime.now(timezone.utc))
    try:
        age_limit = _finite(max_age_minutes, positive=True)
    except ValueError:
        return _unknown("ALERT_FRESHNESS_POLICY_MISSING")
    if isinstance(envelope, list):
        return _unknown("LEGACY_LIST_NO_PROVENANCE", alert_count=len(envelope))
    if not isinstance(envelope, dict) or envelope.get("schema_version") != WARNING_SCHEMA:
        return _unknown("OFFICIAL_ALERT_ENVELOPE_MISSING")
    if envelope.get("request_status") in FAILURE_STATES:
        return _unknown("ALERT_" + envelope["request_status"].upper())
    if envelope.get("request_status") != "success":
        return _unknown("ALERT_QUERY_STATUS_UNKNOWN")
    if envelope.get("provider") != "qweather" or envelope.get("product") != "warning/now":
        return _unknown("WRONG_ALERT_PRODUCT")
    query = envelope.get("query")
    if (not isinstance(query, dict) or query.get("type") != "county" or not expected_spatial_id
            or query.get("spatial_id") != expected_spatial_id or query.get("coverage_confirmed") is not True):
        return _unknown("ALERT_SPATIAL_COVERAGE_UNCONFIRMED")
    if (envelope.get("http_status") != 200 or envelope.get("provider_code") != "200"
            or envelope.get("complete") is not True or isinstance(envelope.get("rejected_item_count"), bool)
            or envelope.get("rejected_item_count") != 0):
        return _unknown("ALERT_RESPONSE_INCOMPLETE")
    if envelope.get("issued_at_source") not in {"provider_updateTime", "provider_generatedAt", "provider_documented_snapshot_time"}:
        return _unknown("ALERT_ISSUANCE_NOT_FROM_PRODUCT")
    digest = envelope.get("response_sha256")
    if not isinstance(digest, str) or not HEX_RE.fullmatch(digest):
        return _unknown("ALERT_RESPONSE_HASH_MISSING")
    try:
        queried, issued, retrieved = (_time(envelope.get(k)) for k in ("queried_at", "issued_at", "retrieved_at"))
        if not issued <= queried <= retrieved <= moment:
            return _unknown("ALERT_TIME_ORDER_INVALID")
        if any((moment - t).total_seconds() / 60 > age_limit for t in (queried, issued)):
            return _unknown("ALERT_STALE")
    except (ValueError, TypeError, OverflowError):
        return _unknown("ALERT_PRODUCT_TIME_UNKNOWN")
    items = envelope.get("items")
    raw_count = envelope.get("raw_item_count")
    if (not isinstance(items, list) or isinstance(raw_count, bool) or not isinstance(raw_count, int)
            or raw_count != len(items)):
        return _unknown("ALERT_ITEMS_INCOMPLETE")
    ids = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or item["id"] in ids:
            return _unknown("ALERT_ITEM_ID_INVALID", alert_count=len(items))
        ids.add(item["id"])
        # 不补造CAP的Likely/Expected；缺少必要事件时间与原始语义时不能完成核对。
        try:
            sent, effective, expires = (_time(item.get(k)) for k in ("sent", "effective", "expires"))
            if not sent <= effective < expires or sent > moment:
                return _unknown("ALERT_ITEM_TIME_INVALID", alert_count=len(items))
        except (ValueError, TypeError, OverflowError):
            return _unknown("ALERT_ITEM_TIME_UNKNOWN", alert_count=len(items))
        if item.get("severity") not in {"Extreme", "Severe", "Moderate", "Minor", "Unknown"}:
            return _unknown("ALERT_SEVERITY_UNKNOWN", alert_count=len(items))
        if item.get("certainty") not in {"Observed", "Likely", "Possible", "Unlikely", "Unknown"}:
            return _unknown("ALERT_CERTAINTY_MISSING", alert_count=len(items))
        if item.get("urgency") not in {"Immediate", "Expected", "Future", "Past", "Unknown"}:
            return _unknown("ALERT_URGENCY_MISSING", alert_count=len(items))
        if item.get("semantic_source") != "provider_values_or_explicit_unknown":
            return _unknown("ALERT_SEMANTICS_IMPUTED", alert_count=len(items))
        if expires <= moment:
            return _unknown("ALERT_EXPIRED_ITEM_REQUIRES_REFRESH", alert_count=len(items))
    marked = bool(items)
    return {"state": "active_alerts" if marked else "queried_clear", "coverage_status": "active_alerts" if marked else "queried_clear",
            "blocks_real_plan": marked, "alert_count": len(items), "reason_codes": [],
            "queried_at": queried.isoformat(), "issued_at": issued.isoformat(),
            "meaning": "有预警时需映射事件与作业条件后重新核对" if marked else "此次完整县域官方查询未返回预警；不是田间或个人健康安全许可"}


def assess_warning_envelope(envelope, *, expected_spatial_id: str,
                            now: str | datetime | None = None,
                            max_age_minutes: float) -> dict:
    """畸形或旧接口结果保持未知，不将异常细节或地点投影到输出。"""
    try:
        return _assess_warning_envelope(envelope, expected_spatial_id=expected_spatial_id,
                                        now=now, max_age_minutes=max_age_minutes)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return _unknown("ALERT_ENVELOPE_MALFORMED")


def _base(now: datetime, mode: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "exported_at": now.isoformat(), "status": "blocked",
            "displayable": False, "label": STATUS_LABELS["blocked"], "mode": mode,
            "audience": "authorized_owner_view", "reason_codes": [], "sessions": [], "tasks": [],
            "alert_state": "unknown", "validation": {"state_match": False, "engine": False, "independent": False},
            "meaning": "安排不等于已经执行或经医学验证安全；本适配器不发送数据、不改变原网站",
            "contains_names_health_or_location": False}


def _blocked(result: dict, code: str, *, expired=False) -> dict:
    result["status"] = "expired" if expired else "blocked"
    result["label"] = STATUS_LABELS[result["status"]]
    result["reason_codes"].append(code)
    return result


def _current_alerts(state: dict, now: datetime) -> tuple[str, str | None]:
    states = []
    age_limit = _finite(state["policy"].get("max_forecast_age_minutes"), positive=True)
    for plot in state["plots"]:
        weather = state["weather"].get(plot["id"], {})
        if weather.get("kind") not in {"forecast", "measured"}:
            return "unknown", "WEATHER_ORIGIN_UNVERIFIED"
        try:
            issued = (validate_run_snapshot(weather, now) if "forecast_run_snapshot" in weather
                      else _time(weather.get("issued_at")))
            if "forecast_run_snapshot" in weather:
                validate_run_location(weather["forecast_run_snapshot"], plot)
            if not 0 <= (now - issued).total_seconds() / 60 <= age_limit:
                return "unknown", "WEATHER_STALE"
        except (ValueError, TypeError, OverflowError):
            return "unknown", "WEATHER_ISSUANCE_UNKNOWN"
        feed = weather.get("alert_feed")
        query_issues = alert_query_issues(feed, now)
        if query_issues:
            return "unknown", query_issues[0][0]
        if (not isinstance(feed, dict) or not isinstance(feed.get("coverage_status"), str)
                or feed["coverage_status"] not in {"queried_clear", "active_alerts"}):
            return "unknown", "OFFICIAL_ALERT_STATUS_UNKNOWN"
        query = feed.get("query")
        if isinstance(query, dict) and query.get("type") == "point" and (
                query.get("latitude") != plot.get("latitude") or query.get("longitude") != plot.get("longitude")):
            return "unknown", "ALERT_QUERY_POINT_NEQ_PLOT"
        # 只有刚通过原响应重建校验的原生快照可没有整体签发时间；旧信封仍须提供。
        if "query_snapshot" not in feed or feed.get("issued_at") is not None:
            try:
                # 事件可以长期有效；查询新鲜度不能错误套到旧事件的签发时刻。
                if _time(feed.get("issued_at")) > now:
                    return "unknown", "OFFICIAL_ALERT_TIME_UNKNOWN"
            except (ValueError, TypeError, OverflowError):
                return "unknown", "OFFICIAL_ALERT_TIME_UNKNOWN"
        states.append(feed["coverage_status"])
    return ("active_alerts" if "active_alerts" in states else "queried_clear" if states else "unknown"), None


def adapt_agriculture_plan(state: dict, plan: dict, *, now: str | datetime | None = None) -> dict:
    """只输出当前state匹配、重新复查通过的安排白名单；个人原始资料不出本机边界。"""
    moment = _time(now or datetime.now(timezone.utc))
    mode = state.get("mode") if isinstance(state, dict) else None
    mode = mode if mode in {"real", "demonstration"} else "unknown"
    result = _base(moment, mode)
    try:
        current = validate_state(state)
        if not isinstance(plan, dict) or plan.get("mode") != mode:
            return _blocked(result, "MODE_MISMATCH")
        if plan.get("valid_until") and _time(plan["valid_until"]) <= moment:
            return _blocked(result, "PLAN_EXPIRED", expired=True)
        if plan.get("stale") is not False or plan.get("displayable") is not True:
            return _blocked(result, "PLAN_NOT_CURRENT")
        if (isinstance(plan.get("state_revision"), bool) or not isinstance(plan.get("state_revision"), int)
                or isinstance(current["revision"], bool) or not isinstance(current["revision"], int)
                or current["revision"] != plan["state_revision"] + 1):
            return _blocked(result, "STATE_REVISION_MISMATCH")
        # 保存计划会把state版本加一；必须同时是当前state中存储的最新原样计划。
        if not current["plans"] or current["plans"][-1] != plan:
            return _blocked(result, "PLAN_NOT_LATEST_SAVED")
        request = plan.get("request")
        if not isinstance(request, dict):
            return _blocked(result, "PLAN_REQUEST_MISSING")
        expected_mode = "demonstration" if mode == "demonstration" else "assistance"
        if request.get("mode") != expected_mode:
            return _blocked(result, "REQUEST_MODE_MISMATCH")
        if plan.get("status") not in {"complete", "partial", "no_plan_found"}:
            return _blocked(result, "PLAN_STATUS_UNKNOWN")
        if _time(plan["created_at"]) > moment:
            return _blocked(result, "PLAN_CREATED_IN_FUTURE")
        if _time(plan["valid_until"]) != _time(request["horizon_end"]) or _time(plan["valid_until"]) <= moment:
            return _blocked(result, "PLAN_EXPIRED_OR_CHANGED_VALIDITY", expired=True)
        _time(request["now"])
        original_now = datetime.fromisoformat(request["now"].replace("Z", "+00:00"))
        rebuilt = build_request(current, now=original_now)
        original_hash = sha256(json_text(request).encode()).hexdigest()
        if original_hash != plan.get("input_sha256") or sha256(json_text(rebuilt).encode()).hexdigest() != original_hash:
            return _blocked(result, "STATE_INPUT_HASH_MISMATCH")
        result["validation"]["state_match"] = True
        verification = plan.get("verification", {})
        if (verification.get("engine", {}).get("valid") is not True
                or verification.get("independent", {}).get("ok", verification.get("independent", {}).get("valid")) is not True):
            return _blocked(result, "STORED_VERIFICATION_NOT_PASSED")
        # 重新验算实际数量、时间轴与限制，不把客户端两个true当成可信结论。
        digest = request_digest(request)
        to_check = {"mode": expected_mode, "request_sha256": digest, "plan_id": "plan-" + digest[:16],
                    "status": plan["status"], "sessions": deepcopy(plan["sessions"]),
                    "task_results": deepcopy(plan["task_results"])}
        checked, independent = verify_plan(request, to_check), check_plan(request, to_check)
        result["validation"].update(engine=checked.get("valid") is True, independent=independent.get("ok", independent.get("valid")) is True)
        if not all(result["validation"].values()):
            return _blocked(result, "RECOMPUTED_VERIFICATION_FAILED")
        if mode == "real":
            ready = readiness(current, now=moment.astimezone(ZoneInfo(current["settings"]["timezone"])))
            if not ready["ready"]:
                return _blocked(result, "CURRENT_READINESS_INCOMPLETE")
            alert_state, problem = _current_alerts(current, moment)
            result["alert_state"] = alert_state
            if problem:
                return _blocked(result, problem)
            if any(_time(session["start"]) <= moment for session in plan["sessions"]):
                return _blocked(result, "PLANNED_START_PASSED_REPLAN_REQUIRED", expired=True)
        else:
            result["alert_state"] = "demonstration"
        catalog = get_community_catalog()
        templates = {t["code"]: t for t in catalog["tasks"]}
        source_tasks = {t["id"]: t for t in current["tasks"]}
        task_numbers = {t["id"]: index + 1 for index, t in enumerate(current["tasks"])}
        person_numbers = {p["id"]: index + 1 for index, p in enumerate([current["profile"], *current["helpers"]])}
        def label(tid):
            task = source_tasks[tid]
            code = task.get("task_code", task.get("operation"))
            if code in GENERIC_TASK_LABELS:
                return GENERIC_TASK_LABELS[code]
            code = TASK_ALIASES.get(code, code)
            return templates.get(code, {}).get("name", "农活任务")
        for session in plan["sessions"]:
            unit = session["unit"]
            if unit not in catalog["unit_labels"]:
                return _blocked(_base(moment, mode), "UNIT_NOT_SUPPORTED_FOR_DISPLAY")
            phases = []
            for phase in session.get("phases", []):
                if phase["kind"] not in {"setup", "outbound", "work", "rest", "wait", "cleanup", "return", "buffer"}:
                    return _blocked(_base(moment, mode), "PHASE_NOT_SUPPORTED_FOR_DISPLAY")
                phases.append({"kind": phase["kind"], "start": _time(phase["start"]).isoformat(), "end": _time(phase["end"]).isoformat()})
            result["sessions"].append({"task_number": task_numbers[session["task_id"]], "task_label": label(session["task_id"]),
                                        "participant_number": person_numbers[session["worker_id"]],
                                        "start": _time(session["start"]).isoformat(), "end": _time(session["end"]).isoformat(),
                                        "quantity": _finite(session["quantity"], positive=True), "unit": unit,
                                        "unit_label": catalog["unit_labels"][unit], "phases": phases})
        for item in plan["task_results"]:
            result["tasks"].append({"task_number": task_numbers[item["task_id"]], "task_label": label(item["task_id"]),
                                    "requested_quantity": _finite(item["requested_quantity"]),
                                    "scheduled_quantity": _finite(item["scheduled_quantity"]),
                                    "unarranged_quantity": _finite(item["remaining_quantity"]), "unit": item["unit"],
                                    "meaning": "已排进时间表的量；不是实际完成量"})
        result.update(status="demonstration" if mode == "demonstration" else "draft", displayable=True,
                      valid_until=_time(plan["valid_until"]).isoformat(),
                      timezone=current["settings"]["timezone"], plan_created_at=_time(plan["created_at"]).isoformat())
        result["label"] = STATUS_LABELS[result["status"]]
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        # 异常细节可能含私有编号、姓名或地名；输出固定原因码而不是str(exc)。
        return _blocked(_base(moment, mode), "MALFORMED_OR_UNVERIFIABLE_INPUT")
