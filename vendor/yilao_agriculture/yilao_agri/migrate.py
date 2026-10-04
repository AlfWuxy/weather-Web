"""旧请求到 schema 1.0 的显式迁移；不升级真实性或审核等级。"""

from __future__ import annotations

from copy import deepcopy

from .models import InputError, validate_request


class MigrationError(InputError):
    """迁移失败；消息含字段路径。不把失败写成已核实。"""


TARGET_SCHEMA = "1.0"

# 真实性：synthetic < forecast < measured。缺省不能发明任一级。
_KIND_RANK = {"synthetic": 0, "forecast": 1, "measured": 2}
# 审核：unknown < illustrative < confirmed < professional_review。confirmed 不能升专业审核。
_REVIEW_RANK = {"unknown": 0, "illustrative": 1, "confirmed": 2, "professional_review": 3}
# demonstration 更宽松（允许 synthetic/illustrative），不能从影子/辅助自动改过去。
_MODE_RANK = {"shadow": 0, "assistance": 0, "demonstration": 1}
_STATE_RANK = {"unknown": 0, "stop": 0, "clear": 1}

# schema 1.0 闭集。1.1 旁路键抽出 sidecar，不写入请求。
_REQUEST_KEYS = frozenset({
    "schema_version", "mode", "now", "horizon_start", "horizon_end", "timezone",
    "step_minutes", "beam_width", "max_search_states", "plots", "workers",
    "resources", "tasks", "weather", "policy",
})
_PLOT_KEYS = frozenset({"id", "region_id", "latitude", "longitude", "environment", "conditions"})
_WORKER_KEYS = frozenset({
    "id", "state", "availability", "limits", "used_active_minutes_by_date",
    "initial_rest_confirmed", "role", "worker_profile",
})
_LIMIT_KEYS = frozenset({
    "max_active_minutes_per_day", "max_continuous_active_minutes", "min_rest_minutes",
    "max_load_kg", "forbidden_tags", "source", "review_status",
})
_RESOURCE_KEYS = frozenset({"id", "kind", "capacity", "availability", "occupation"})
_TASK_KEYS = frozenset({
    "id", "plot_id", "crop_id", "stage", "operation", "method", "remaining_quantity",
    "rates", "earliest_start", "deadline", "deadline_source", "priority", "divisible",
    "min_chunk_minutes", "quantity_step", "depends_on", "required_resources", "tags",
    "load_per_trip_kg", "session", "once", "agronomy", "deadline_basis",
    "operation_scope", "parallel_within_task", "crop_alias", "crop_alias_resolution", "wait_duty",
})
_QUANTITY_KEYS = frozenset({"value", "unit"})
_RATE_KEYS = frozenset({"unit", "low", "high", "scope", "source"})
_SESSION_KEYS = frozenset({
    "setup_minutes", "outbound_minutes", "return_minutes", "cleanup_minutes", "buffer_minutes",
})
_AGRONOMY_KEYS = frozenset({"status", "source", "conditions", "weather_limits"})
_WEATHER_KEYS = frozenset({"source", "issued_at", "kind", "environment", "wbgt_method", "records"})
_RECORD_KEYS = frozenset({
    "start", "end", "temperature_c", "relative_humidity_pct", "wind_m_s",
    "shortwave_w_m2", "precipitation_mm", "daylight", "hazards", "wbgt_c",
})
_POLICY_KEYS = frozenset({
    "source", "review_status", "max_forecast_age_minutes", "max_forecast_horizon_hours",
    "require_daylight", "blocked_hazards", "required_resource_kinds", "weather_limits",
})
_INTERVAL_KEYS = frozenset({"start", "end"})
_TASK_WEATHER_LIMIT_KEYS = frozenset({
    "max_precipitation_mm", "max_wind_m_s", "max_temperature_c", "min_temperature_c",
})
_POLICY_WEATHER_LIMIT_KEYS = frozenset({
    "max_precipitation_mm", "max_wind_m_s", "max_temperature_c", "max_wbgt_c",
})

_SIDECAR_REQUEST = frozenset({"rule_evidence", "alert_feed"})
_SIDECAR_WORKER = frozenset({
    "state_observed_at", "state_source", "state_reporter_role", "initial_rest_confirmed_at",
})
_SIDECAR_LIMITS = frozenset({
    "issued_at", "valid_until", "validity_kind", "reviewer_role",
    "max_load_presence", "forbidden_tags_presence", "rule_version",
})
_SIDECAR_AGRONOMY = frozenset({"reviewed_at", "rule_version", "reviewer_role", "packet_id"})
_SIDECAR_POLICY = frozenset({"rule_version", "reviewer_role"})
_SIDECAR_WEATHER = frozenset({"alert_feed", "coverage_status", "hazards_coverage"})
_SIDECAR_RECORD = frozenset({"alert_item_ids", "coverage_status", "hazard_item_ids"})

_DECLARATION_CLASS = {
    "unknown": "unknown",
    "illustrative": "illustrative",
    "confirmed": "provider_declared",
}


def _fail(path, message):
    raise MigrationError(f"{path}: {message}")


def _missing(obj, key):
    return isinstance(obj, dict) and key not in obj


def _rank(table, value, omitted_rank):
    if value is None:
        return omitted_rank
    if not isinstance(value, str):
        return omitted_rank
    return table.get(value, omitted_rank)


def _action(actions, path, action, source=None, target=None, note=None):
    item = {"path": path, "action": action}
    if source is not None:
        item["from"] = source
    if target is not None:
        item["to"] = target
    if note:
        item["note"] = note
    actions.append(item)


def _pop_known(obj, keys, path, extracted):
    if not isinstance(obj, dict):
        return
    for key in list(obj.keys()):
        if key in keys:
            extracted.append({"path": f"{path}.{key}" if path else key, "value": deepcopy(obj.pop(key))})


def _strip_keys(obj, allowed, path, actions):
    if not isinstance(obj, dict):
        return
    for key in list(obj.keys()):
        if key not in allowed:
            _action(actions, f"{path}.{key}" if path else key, "drop_unknown_field",
                    source=obj[key], note="展示或旁路字段，不进入 schema 1.0 请求")
            del obj[key]


def _unknown_in(obj, allowed, path, found):
    if not isinstance(obj, dict):
        return
    for key in obj:
        if key not in allowed:
            found.append(f"{path}.{key}" if path else key)


def _walk_unknown(request, found):
    _unknown_in(request, _REQUEST_KEYS, "request", found)
    for i, plot in enumerate(request.get("plots") or []):
        _unknown_in(plot, _PLOT_KEYS, f"plots[{i}]", found)
    for i, worker in enumerate(request.get("workers") or []):
        if not isinstance(worker, dict):
            continue
        _unknown_in(worker, _WORKER_KEYS, f"workers[{i}]", found)
        _unknown_in(worker.get("limits"), _LIMIT_KEYS, f"workers[{i}].limits", found)
        for j, interval in enumerate(worker.get("availability") or []):
            _unknown_in(interval, _INTERVAL_KEYS, f"workers[{i}].availability[{j}]", found)
    for i, resource in enumerate(request.get("resources") or []):
        if not isinstance(resource, dict):
            continue
        _unknown_in(resource, _RESOURCE_KEYS, f"resources[{i}]", found)
        for j, interval in enumerate(resource.get("availability") or []):
            _unknown_in(interval, _INTERVAL_KEYS, f"resources[{i}].availability[{j}]", found)
    for i, task in enumerate(request.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        _unknown_in(task, _TASK_KEYS, f"tasks[{i}]", found)
        _unknown_in(task.get("remaining_quantity"), _QUANTITY_KEYS, f"tasks[{i}].remaining_quantity", found)
        _unknown_in(task.get("session"), _SESSION_KEYS, f"tasks[{i}].session", found)
        _unknown_in(task.get("agronomy"), _AGRONOMY_KEYS, f"tasks[{i}].agronomy", found)
        agronomy = task.get("agronomy") if isinstance(task.get("agronomy"), dict) else {}
        _unknown_in(agronomy.get("weather_limits"), _TASK_WEATHER_LIMIT_KEYS,
                    f"tasks[{i}].agronomy.weather_limits", found)
        rates = task.get("rates")
        if isinstance(rates, dict):
            for worker_id, rate in rates.items():
                _unknown_in(rate, _RATE_KEYS, f"tasks[{i}].rates.{worker_id}", found)
    weather = request.get("weather")
    if isinstance(weather, dict):
        for plot_id, block in weather.items():
            if not isinstance(block, dict):
                continue
            _unknown_in(block, _WEATHER_KEYS, f"weather.{plot_id}", found)
            for j, record in enumerate(block.get("records") or []):
                _unknown_in(record, _RECORD_KEYS, f"weather.{plot_id}.records[{j}]", found)
    policy = request.get("policy")
    if isinstance(policy, dict):
        _unknown_in(policy, _POLICY_KEYS, "policy", found)
        _unknown_in(policy.get("weather_limits"), _POLICY_WEATHER_LIMIT_KEYS, "policy.weather_limits", found)


def _strip_unknown(request, actions):
    _strip_keys(request, _REQUEST_KEYS, "request", actions)
    for i, plot in enumerate(request.get("plots") or []):
        _strip_keys(plot, _PLOT_KEYS, f"plots[{i}]", actions)
    for i, worker in enumerate(request.get("workers") or []):
        if not isinstance(worker, dict):
            continue
        _strip_keys(worker, _WORKER_KEYS, f"workers[{i}]", actions)
        _strip_keys(worker.get("limits"), _LIMIT_KEYS, f"workers[{i}].limits", actions)
        for j, interval in enumerate(worker.get("availability") or []):
            _strip_keys(interval, _INTERVAL_KEYS, f"workers[{i}].availability[{j}]", actions)
    for i, resource in enumerate(request.get("resources") or []):
        if not isinstance(resource, dict):
            continue
        _strip_keys(resource, _RESOURCE_KEYS, f"resources[{i}]", actions)
        for j, interval in enumerate(resource.get("availability") or []):
            _strip_keys(interval, _INTERVAL_KEYS, f"resources[{i}].availability[{j}]", actions)
    for i, task in enumerate(request.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        _strip_keys(task, _TASK_KEYS, f"tasks[{i}]", actions)
        _strip_keys(task.get("remaining_quantity"), _QUANTITY_KEYS, f"tasks[{i}].remaining_quantity", actions)
        _strip_keys(task.get("session"), _SESSION_KEYS, f"tasks[{i}].session", actions)
        _strip_keys(task.get("agronomy"), _AGRONOMY_KEYS, f"tasks[{i}].agronomy", actions)
        agronomy = task.get("agronomy") if isinstance(task.get("agronomy"), dict) else {}
        _strip_keys(agronomy.get("weather_limits"), _TASK_WEATHER_LIMIT_KEYS,
                    f"tasks[{i}].agronomy.weather_limits", actions)
        rates = task.get("rates")
        if isinstance(rates, dict):
            for worker_id, rate in rates.items():
                _strip_keys(rate, _RATE_KEYS, f"tasks[{i}].rates.{worker_id}", actions)
    weather = request.get("weather")
    if isinstance(weather, dict):
        for plot_id, block in weather.items():
            if not isinstance(block, dict):
                continue
            _strip_keys(block, _WEATHER_KEYS, f"weather.{plot_id}", actions)
            for j, record in enumerate(block.get("records") or []):
                _strip_keys(record, _RECORD_KEYS, f"weather.{plot_id}.records[{j}]", actions)
    policy = request.get("policy")
    if isinstance(policy, dict):
        _strip_keys(policy, _POLICY_KEYS, "policy", actions)
        _strip_keys(policy.get("weather_limits"), _POLICY_WEATHER_LIMIT_KEYS, "policy.weather_limits", actions)


def _extract_sidecars(request):
    extracted = []
    _pop_known(request, _SIDECAR_REQUEST, "request", extracted)
    for i, worker in enumerate(request.get("workers") or []):
        if not isinstance(worker, dict):
            continue
        _pop_known(worker, _SIDECAR_WORKER, f"workers[{i}]", extracted)
        _pop_known(worker.get("limits"), _SIDECAR_LIMITS, f"workers[{i}].limits", extracted)
    for i, task in enumerate(request.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        agronomy = task.get("agronomy")
        _pop_known(agronomy, _SIDECAR_AGRONOMY, f"tasks[{i}].agronomy", extracted)
    _pop_known(request.get("policy") if isinstance(request.get("policy"), dict) else None,
               _SIDECAR_POLICY, "policy", extracted)
    weather = request.get("weather")
    if isinstance(weather, dict):
        for plot_id, block in weather.items():
            if not isinstance(block, dict):
                continue
            _pop_known(block, _SIDECAR_WEATHER, f"weather.{plot_id}", extracted)
            for j, record in enumerate(block.get("records") or []):
                _pop_known(record, _SIDECAR_RECORD, f"weather.{plot_id}.records[{j}]", extracted)
    return extracted


def _collect_trust(obj):
    """收集不能被迁移抬高的真实性/审核/休息声明。"""
    if not isinstance(obj, dict):
        return {
            "mode": None,
            "kinds": {},
            "reviews": {},
            "states": {},
            "rest": {},
            "hazards": {},
        }
    kinds = {}
    hazards = {}
    weather = obj.get("weather")
    if isinstance(weather, dict):
        for plot_id, block in weather.items():
            if not isinstance(block, dict):
                continue
            kinds[str(plot_id)] = block.get("kind") if "kind" in block else None
            for j, record in enumerate(block.get("records") or []):
                if not isinstance(record, dict):
                    continue
                key = f"{plot_id}#{record.get('start', j)}"
                hazards[key] = deepcopy(record["hazards"]) if "hazards" in record else None
    reviews = {}
    states = {}
    rest = {}
    for i, worker in enumerate(obj.get("workers") or []):
        if not isinstance(worker, dict):
            continue
        wid = worker.get("id", i)
        states[str(wid)] = worker.get("state") if "state" in worker else None
        rest[str(wid)] = worker.get("initial_rest_confirmed") if "initial_rest_confirmed" in worker else None
        limits = worker.get("limits") if isinstance(worker.get("limits"), dict) else {}
        reviews[f"workers[{i}].limits.review_status"] = (
            limits.get("review_status") if "review_status" in limits else None
        )
    for i, task in enumerate(obj.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        agronomy = task.get("agronomy") if isinstance(task.get("agronomy"), dict) else {}
        reviews[f"tasks[{i}].agronomy.status"] = agronomy.get("status") if "status" in agronomy else None
    policy = obj.get("policy") if isinstance(obj.get("policy"), dict) else {}
    reviews["policy.review_status"] = policy.get("review_status") if "review_status" in policy else None
    return {
        "mode": obj.get("mode") if "mode" in obj else None,
        "kinds": kinds,
        "reviews": reviews,
        "states": states,
        "rest": rest,
        "hazards": hazards,
    }


def _refuse_upgrades(before, after):
    old_mode, new_mode = before["mode"], after["mode"]
    if _rank(_MODE_RANK, new_mode, 0) > _rank(_MODE_RANK, old_mode, 0):
        _fail("mode", "不能把运行模式升级为 demonstration 以放行合成参数")
    for plot_id, old_kind in before["kinds"].items():
        new_kind = after["kinds"].get(plot_id)
        if old_kind is None and new_kind is not None:
            _fail(f"weather.{plot_id}.kind", "缺 weather.kind 不能发明真实性等级")
        if _rank(_KIND_RANK, new_kind, -1) > _rank(_KIND_RANK, old_kind, -1):
            _fail(f"weather.{plot_id}.kind", "不能把合成天气升级为预报或实测")
    for path, old_status in before["reviews"].items():
        new_status = after["reviews"].get(path)
        if _rank(_REVIEW_RANK, new_status, 0) > _rank(_REVIEW_RANK, old_status, 0):
            _fail(path, f"不能把审核等级从 {old_status or '省略/unknown'} 升级为 {new_status}")
    for wid, old_state in before["states"].items():
        new_state = after["states"].get(wid)
        if _rank(_STATE_RANK, new_state, 0) > _rank(_STATE_RANK, old_state, 0):
            _fail(f"workers.{wid}.state", "不能把人员状态升级为 clear")
    for wid, old_rest in before["rest"].items():
        new_rest = after["rest"].get(wid)
        if new_rest is True and old_rest is not True:
            _fail(f"workers.{wid}.initial_rest_confirmed",
                  "不能把 initial_rest_confirmed 从省略/false 改为 true")
    for key, old_hazards in before["hazards"].items():
        new_hazards = after["hazards"].get(key)
        if old_hazards is None and new_hazards is not None:
            _fail("hazards", "缺 hazards 不能填空数组冒充已核对无预警")
        if old_hazards is not None and new_hazards != old_hazards:
            _fail("hazards", "不能改写已有危险天气标记")


def _apply_defaults(request, actions, target_schema=TARGET_SCHEMA):
    if _missing(request, "schema_version"):
        request["schema_version"] = TARGET_SCHEMA
        _action(actions, "schema_version", "default", source=None, target=TARGET_SCHEMA,
                note="省略版本按 1.0 兼容，不是升级到更新契约")
    version = request.get("schema_version")
    if version not in {"1.0", "1.1"} or (version == "1.1" and target_schema == "1.0"):
        _fail("schema_version", "不支持该版本或向 1.0 降级；人员有效期不可丢弃")
    if version != target_schema:
        request["schema_version"] = target_schema
        _action(actions, "schema_version", "explicit_upgrade", source=version, target=target_schema,
                note="仅显式升级结构；缺人员资料保持 unfilled，不能发明有效期")
    if _missing(request, "mode"):
        request["mode"] = "shadow"
        _action(actions, "mode", "default", source=None, target="shadow",
                note="省略 mode 按 shadow，不是 demonstration")
    for i, worker in enumerate(request.get("workers") or []):
        if not isinstance(worker, dict):
            continue
        path = f"workers[{i}]"
        if _missing(worker, "state"):
            worker["state"] = "unknown"
            _action(actions, f"{path}.state", "default", source=None, target="unknown",
                    note="省略人员状态按 unknown，不是 clear")
        if _missing(worker, "initial_rest_confirmed"):
            worker["initial_rest_confirmed"] = False
            _action(actions, f"{path}.initial_rest_confirmed", "default", source=None, target=False,
                    note="省略按 false，首段预留休息；不能当作已休息")
        worker.setdefault("limits", {})
        limits = worker["limits"]
        if isinstance(limits, dict) and _missing(limits, "review_status"):
            limits["review_status"] = "unknown"
            _action(actions, f"{path}.limits.review_status", "default", source=None, target="unknown",
                    note="省略审核按 unknown，不是 confirmed")
    for i, task in enumerate(request.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        task.setdefault("agronomy", {})
        agronomy = task["agronomy"]
        if isinstance(agronomy, dict) and _missing(agronomy, "status"):
            agronomy["status"] = "unknown"
            _action(actions, f"tasks[{i}].agronomy.status", "default", source=None, target="unknown",
                    note="省略农艺审核按 unknown，不是 confirmed")
    request.setdefault("policy", {})
    policy = request["policy"]
    if isinstance(policy, dict) and _missing(policy, "review_status"):
        policy["review_status"] = "unknown"
        _action(actions, "policy.review_status", "default", source=None, target="unknown",
                note="省略政策审核按 unknown，不是 confirmed")


def _refuse_invented_authenticity(request):
    weather = request.get("weather")
    if not isinstance(weather, dict):
        return
    for plot_id, block in weather.items():
        if not isinstance(block, dict):
            continue
        path = f"weather.{plot_id}"
        if "kind" not in block:
            _fail(f"{path}.kind", "缺 weather.kind 不能发明真实性等级")
        for j, record in enumerate(block.get("records") or []):
            if not isinstance(record, dict):
                continue
            if "hazards" not in record:
                _fail(f"{path}.records[{j}].hazards",
                      "缺 hazards 不能填空数组冒充已核对无预警")


def _declaration_class(status):
    if status == "confirmed":
        return "provider_declared"
    return _DECLARATION_CLASS.get(status, "unknown")


def _build_sidecars(request, extracted):
    rule_evidence = []
    for i, worker in enumerate(request.get("workers") or []):
        if not isinstance(worker, dict):
            continue
        limits = worker.get("limits") if isinstance(worker.get("limits"), dict) else {}
        status = limits.get("review_status", "unknown")
        rule_evidence.append({
            "binds_to": {"object_path": f"workers[{i}].limits", "rule_kind": "worker_limits"},
            "review": {
                "schema_review_status": status,
                "declaration_class": _declaration_class(status),
                "professional_review": False,
                "evidence_type": "IMPLEMENTATION_TEST",
            },
            "migration": {
                "from": f"schema 1.0 review_status={status}",
                "to": f"declaration_class={_declaration_class(status)}",
                "note": "confirmed 无包只能是 provider_declared，不能升 professional_review",
            },
        })
    for i, task in enumerate(request.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        agronomy = task.get("agronomy") if isinstance(task.get("agronomy"), dict) else {}
        status = agronomy.get("status", "unknown")
        rule_evidence.append({
            "binds_to": {"object_path": f"tasks[{i}].agronomy", "rule_kind": "agronomy"},
            "review": {
                "schema_review_status": status,
                "declaration_class": _declaration_class(status),
                "professional_review": False,
                "evidence_type": "IMPLEMENTATION_TEST",
            },
            "migration": {
                "from": f"schema 1.0 agronomy.status={status}",
                "to": f"declaration_class={_declaration_class(status)}",
                "note": "confirmed 无包只能是 provider_declared，不能升 professional_review",
            },
        })
    policy = request.get("policy") if isinstance(request.get("policy"), dict) else {}
    status = policy.get("review_status", "unknown")
    rule_evidence.append({
        "binds_to": {"object_path": "policy", "rule_kind": "policy"},
        "review": {
            "schema_review_status": status,
            "declaration_class": _declaration_class(status),
            "professional_review": False,
            "evidence_type": "IMPLEMENTATION_TEST",
        },
        "migration": {
            "from": f"schema 1.0 policy.review_status={status}",
            "to": f"declaration_class={_declaration_class(status)}",
            "note": "confirmed 无包只能是 provider_declared，不能升 professional_review",
        },
    })
    weather_alert = []
    weather = request.get("weather") if isinstance(request.get("weather"), dict) else {}
    for plot_id, block in weather.items():
        if not isinstance(block, dict):
            continue
        records = []
        for record in block.get("records") or []:
            if not isinstance(record, dict):
                continue
            hazards = record.get("hazards")
            if hazards == []:
                coverage = "empty_declared_not_queried_clear"
            elif isinstance(hazards, list) and hazards:
                coverage = "marked_not_verified"
            else:
                coverage = "unknown"
            records.append({
                "start": record.get("start"),
                "hazards": deepcopy(hazards),
                "hazards_coverage": coverage,
                "coverage_status": "not_inferred",
            })
        weather_alert.append({
            "plot_id": plot_id,
            "kind": block.get("kind"),
            "records": records,
            "note": "空数组不升为 queried_clear；weather.kind 保持原值",
        })
    used_load = []
    for i, worker in enumerate(request.get("workers") or []):
        if not isinstance(worker, dict):
            continue
        used = worker.get("used_active_minutes_by_date")
        if used == {} or _missing(worker, "used_active_minutes_by_date"):
            used_load.append({
                "worker_id": worker.get("id"),
                "path": f"workers[{i}].used_active_minutes_by_date",
                "status": "legacy_unspecified",
                "active_minutes": None,
                "note": "空对象/省略不是已核实零负荷",
            })
    return {
        "rule_evidence": rule_evidence,
        "weather_alert": weather_alert,
        "occurred_load": used_load,
        "extracted_fields": extracted,
    }


def migrate_request(raw, *, drop_unknown_fields=False, target_schema=TARGET_SCHEMA) -> dict:
    """显式迁移到 schema 1.0。不修改原对象，不升级真实性或审核等级。"""
    if target_schema not in {"1.0", "1.1"}:
        _fail("target_schema", "目标只能为 schema 1.0 或 1.1")
    if not isinstance(raw, dict):
        _fail("request", "必须为对象")
    original_trust = _collect_trust(raw)
    request = deepcopy(raw)
    actions = []
    extracted = _extract_sidecars(request)
    if extracted:
        _action(actions, "sidecars", "extract_1_1_fields",
                note="旁路字段移出请求，不用于抬高 schema 1.0 审核或真实性")
    if "schema_version" in request and request["schema_version"] is None:
        _fail("schema_version", "显式 null 不按省略处理，也不能自动升级")
    if "mode" in request and request["mode"] is None:
        _fail("mode", "显式 null 不按省略处理，也不能改成 demonstration")
    _apply_defaults(request, actions, target_schema)
    _refuse_invented_authenticity(request)
    unknown = []
    _walk_unknown(request, unknown)
    if unknown and not drop_unknown_fields:
        _fail(unknown[0], "未知字段须 --drop-unknown-fields 显式丢弃，迁移器不会默默删除")
    if unknown and drop_unknown_fields:
        _strip_unknown(request, actions)
    mid_trust = _collect_trust(request)
    _refuse_upgrades(original_trust, mid_trust)
    validated = validate_request(request)
    _refuse_upgrades(original_trust, _collect_trust(validated))
    sidecars = _build_sidecars(validated, extracted)
    # 再确认 sidecar 没有把 confirmed 写成专业审核。
    for item in sidecars["rule_evidence"]:
        if item["review"].get("professional_review"):
            _fail(item["binds_to"]["object_path"], "禁止把 confirmed 升级为 professional_review")
        if item["review"].get("declaration_class") == "professional_review":
            _fail(item["binds_to"]["object_path"], "禁止把 confirmed 升级为 professional_review")
        if item["review"].get("schema_review_status") == "confirmed":
            if item["review"].get("declaration_class") != "provider_declared":
                _fail(item["binds_to"]["object_path"], "confirmed 无包只能导出为 provider_declared")
    for item in sidecars["weather_alert"]:
        for record in item.get("records") or []:
            if record.get("coverage_status") == "queried_clear":
                _fail(f"weather.{item['plot_id']}", "空数组不能升为 queried_clear")
            if record.get("hazards_coverage") == "queried_clear":
                _fail(f"weather.{item['plot_id']}", "空数组不能升为 queried_clear")
    report = {
        "from_schema": raw.get("schema_version") if isinstance(raw, dict) else None,
        "to_schema": target_schema,
        "actions": actions,
        "input_mutated": False,
        "structurally_valid": True,
        "authenticity_preserved": original_trust["kinds"],
        "review_preserved": original_trust["reviews"],
        "meaning": "仅结构迁移与保守缺省；structurally_valid 不是出工许可、专业审核或健康安全",
    }
    return {
        "request": validated,
        "report": report,
        "sidecars": sidecars,
        "meaning": f"显式迁移到 schema {target_schema}。未升级天气真实性或审核等级。不是专业审核、出工许可或个人健康安全。",
    }
