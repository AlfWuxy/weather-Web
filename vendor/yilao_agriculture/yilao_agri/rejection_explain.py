"""把排程拒绝原因关联到具体「任务 × 人 × 时段 × 输入字段」。

只做归因与措辞，不改变任何硬约束判定：是否拒绝、拒绝谁的候选，
仍由 engine / environment 作出。本模块不新增放宽、不删除既有拒绝。

三条归因纪律（对应 R15-A07 目标）：
1. 每条拒绝都带 task_id；凡与具体候选有关的，还带 worker_id 与/或 window_start
   （具体人、任务、时段），并带 field 指向需要核对的输入字段。
2. 只有与具体人、时段无关的结构性原因（task_structural）才可能成为任务的
   「全局原因」(global_cause)。局部候选失败（candidate_local）与个人级原因
   （worker_level）只逐条列出，永不升级为全局原因，也不据此断言任务无法完成。
3. 同一 (任务, 原因码, 人, 时段) 的重复只累加计数，不制造多个「原因」。
"""

from __future__ import annotations

# 归因层级
SCOPE_TASK_STRUCTURAL = "task_structural"   # 与具体人/时段无关：输入或任务整体缺陷
SCOPE_WORKER_LEVEL = "worker_level"         # 绑定到某个人、跨时段（如本人限制未确认）
SCOPE_CANDIDATE_LOCAL = "candidate_local"   # 绑定到某个「人 × 时段」候选

# 原因码 -> 需要核对的输入字段。给出的只是定位提示，不是判定依据。
FIELD_BY_CODE = {
    # —— 任务级（输入整体）——
    "NO_WORKER": "workers",
    "DEPENDENCY_INCOMPLETE": "tasks[].depends_on",
    "INTEGER_UNIT_SPLIT": "tasks[].remaining_quantity",
    "TASK_PARALLELISM": "tasks[].divisible（并行依据）",
    "TASK_TAGS_INVALID": "tasks[].tags",
    "TASK_TAGS_UNKNOWN": "tasks[].tags",
    "TASK_LOAD_UNKNOWN": "tasks[].load_per_trip_kg",
    "PLOT_UNKNOWN": "tasks[].plot_id / plots[]",
    "PLOT_ENVIRONMENT_UNKNOWN": "plots[].environment",
    "MODE_UNKNOWN": "mode",
    "DEADLINE_SOURCE_UNKNOWN": "tasks[].deadline_source",
    "HORIZON_INVALID": "horizon_start/horizon_end",
    "INTERVAL_TIME_INVALID": "interval 起止时间",
    "INTERVAL_EMPTY": "interval 起止时间",
    "OUTSIDE_PLANNING_HORIZON": "horizon_start/horizon_end",
    "POLICY_UNCONFIRMED": "policy.review_status",
    "POLICY_SOURCE_UNKNOWN": "policy.source",
    "AGRONOMY_UNCONFIRMED": "tasks[].agronomy.status",
    "AGRONOMY_SOURCE_UNKNOWN": "tasks[].agronomy.source",
    "AGRONOMY_STAGE_UNKNOWN": "tasks[].stage",
    "AGRONOMY_CONDITIONS_UNKNOWN": "tasks[].agronomy.conditions",
    "AGRONOMY_CONDITION_UNKNOWN": "tasks[].agronomy.conditions",
    "AGRONOMY_CONDITION_MISMATCH": "tasks[].agronomy.conditions",
    # 天气与网格（地块级输入）
    "WEATHER_MISSING": "weather[plot]",
    "WEATHER_SOURCE_UNKNOWN": "weather[plot].source",
    "WEATHER_KIND_UNKNOWN": "weather[plot].kind",
    "WEATHER_ENVIRONMENT_MISMATCH": "weather[plot].environment",
    "WEATHER_ISSUED_IN_FUTURE": "weather[plot].issued_at",
    "WEATHER_ISSUED_TIME_UNKNOWN": "weather[plot].issued_at",
    "WEATHER_STALE": "weather[plot].issued_at",
    "WEATHER_TIME_POLICY_UNKNOWN": "policy.max_forecast_age_minutes",
    "FORECAST_HORIZON_EXCEEDED": "policy.max_forecast_horizon_hours",
    "MEASURED_WEATHER_IS_NOT_FORECAST": "weather[plot].kind",
    "SYNTHETIC_WEATHER_FORBIDDEN": "weather[plot].kind",
    "WEATHER_RECORDS_INVALID": "weather[plot].records",
    "WEATHER_RECORDS_OVERLAP": "weather[plot].records",
    "WEATHER_RECORD_INTERVAL_INVALID": "weather[plot].records",
    "WEATHER_COVERAGE_GAP": "weather[plot].records",
    "WEATHER_VALUE_UNKNOWN": "weather[plot].records",
    "MEASURED_RECORD_NOT_AVAILABLE": "weather[plot].records",
    "MEASURED_RECORD_STALE": "weather[plot].issued_at",
    "WBGT_PROVENANCE_UNKNOWN": "weather[plot].wbgt_method",
    "WBGT_VALUE_INVALID": "weather[plot].records[].wbgt_c",
    "GRID_CELL_ID_MISSING": "weather[plot].grid.cell_id",
    "GRID_COORDINATE_UNKNOWN": "plots[].latitude/longitude",
    "GRID_DECLARATION_INVALID": "weather[plot].grid",
    "GRID_DISTANCE_UNKNOWN": "weather[plot].grid",
    "GRID_ENVIRONMENT_MISMATCH": "weather[plot].grid.environment",
    "GRID_ENVIRONMENT_UNKNOWN": "weather[plot].grid.environment",
    "GRID_KIND_UNKNOWN": "weather[plot].grid.kind",
    "GRID_PLOT_DISTANCE_EXCEEDED": "policy.grid_plot_distance_tolerance_km",
    "GRID_PLOT_OUTSIDE_CELL": "weather[plot].grid",
    "GRID_REGION_NOT_PLOT_LEVEL": "weather[plot].grid",
    "GRID_RESOLUTION_UNKNOWN": "weather[plot].grid.resolution_km",
    "GRID_SOURCE_UNKNOWN": "weather[plot].grid.source",
    "GRID_TOLERANCE_SOURCE_UNKNOWN": "policy 网格容差来源",
    "GRID_TOLERANCE_UNCONFIRMED": "policy 网格容差",
    "GRID_TOLERANCE_UNKNOWN": "policy 网格容差",
    "PLOT_COORDINATE_UNKNOWN": "plots[].latitude/longitude",
    # 日照 / 灾害 / 暴露声明
    "DAYLIGHT_POLICY_UNKNOWN": "policy.daylight",
    "DAYLIGHT_REQUIRED": "policy.daylight",
    "DAYLIGHT_UNKNOWN": "policy.daylight",
    "HAZARD_POLICY_UNKNOWN": "policy.hazard",
    "HEAT_SCREENING_RULE_MISSING": "policy.heat_screening",
    "UNREVIEWED_HAZARD": "weather[plot].hazards",
    "BLOCKED_HAZARD": "weather[plot].hazards",
    "ALERT_FEED_INVALID": "active_alerts",
    "ALERT_FEED_MISSING": "active_alerts",
    "ENVIRONMENT_METRIC_OVERFLOW": "weather[plot].records",
    "EXPOSURE_DECLARATION_EMPTY": "exposure",
    "EXPOSURE_DECLARATION_INVALID": "exposure",
    "EXPOSURE_SCOPE_MISMATCH": "exposure.scope",
    "EXPOSURE_SCOPE_UNKNOWN": "exposure.scope",
    # 高风险操作范围（按显式审核范围开放）
    "HIGH_RISK_CLASSES": "tasks[].operation / policy.operation_scope",
    "OPERATION_SCOPE_CLASSES_INVALID": "policy.operation_scope",
    "OPERATION_SCOPE_CLASSES_MISMATCH": "policy.operation_scope",
    "OPERATION_SCOPE_SOURCE_UNKNOWN": "policy.operation_scope.source",
    "OPERATION_SCOPE_UNREVIEWED": "policy.operation_scope.review_status",
    "DOSAGE_TOKENS": "tasks[].operation 文本",
    "MANURE_TOKENS": "tasks[].operation 文本",
    "PESTICIDE_TOKENS": "tasks[].operation 文本",
    "DOSAGE_PERMISSION_WITHHELD": "tasks[].operation 文本",
    # —— 个人级（跨时段）——
    "MISSING_WORKER_RATE": "workers[].method 用时依据",
    "RATE_SOURCE_UNKNOWN": "workers[] 速率来源",
    "WHOLE_SESSION_RATE": "workers[] 速率口径",
    "CANDIDATE_ESTIMATE_CLOSED": "workers[] 用时依据",
    "WORKER_STATE_NOT_CLEAR": "workers[].state",
    "WORKER_LIMITS_UNCONFIRMED": "workers[].limits",
    "WORKER_LIMITS_SOURCE_UNKNOWN": "workers[].limits.source",
    "WORKER_TAG_FORBIDDEN": "workers[].limits.forbidden_tags",
    "WORKER_LOAD_EXCEEDED": "workers[].limits.max_load_kg",
    "WORKER_LOAD_LIMIT_UNKNOWN": "workers[].limits.max_load_kg",
    # —— 候选级（人 × 时段）——
    "RESOURCE_UNAVAILABLE": "resources[].availability",
    "RESOURCE_CAPACITY": "resources[].capacity",
    "MISSING_RESOURCE_KIND": "policy.required_resource_kinds",
    "WORKER_UNAVAILABLE": "workers[].availability",
    "WORKER_CONFLICT_OR_REST": "workers[].availability / limits.min_rest_minutes",
    "WORKER_OVERLAP": "workers[].availability",
    "WORKER_REST_INSUFFICIENT": "workers[].limits.min_rest_minutes",
    "DAILY_ACTIVE_LIMIT": "workers[].limits.max_active_minutes_per_day",
    "DEADLINE_OR_HORIZON": "tasks[].deadline / horizon",
}

# 即使在 (人, 时段) 上下文里被记录，语义仍属任务/输入整体；归因时强制为结构性，
# 避免把「输入整体缺陷」误报成某人某时段的局部候选失败。
FORCE_TASK_CODES = frozenset(code for code in FIELD_BY_CODE
                             if code not in {
                                 "MISSING_WORKER_RATE", "RATE_SOURCE_UNKNOWN", "WHOLE_SESSION_RATE",
                                 "CANDIDATE_ESTIMATE_CLOSED", "WORKER_STATE_NOT_CLEAR",
                                 "WORKER_LIMITS_UNCONFIRMED", "WORKER_LIMITS_SOURCE_UNKNOWN",
                                 "WORKER_TAG_FORBIDDEN", "WORKER_LOAD_EXCEEDED",
                                 "WORKER_LOAD_LIMIT_UNKNOWN", "RESOURCE_UNAVAILABLE",
                                 "RESOURCE_CAPACITY", "MISSING_RESOURCE_KIND",
                                 "WORKER_UNAVAILABLE", "WORKER_CONFLICT_OR_REST", "WORKER_OVERLAP", "WORKER_REST_INSUFFICIENT",
                                 "DAILY_ACTIVE_LIMIT", "DEADLINE_OR_HORIZON",
                             })

# 绑定到某个人、跨时段的原因码；记录时即使带时段也归为个人级。
FORCE_WORKER_CODES = frozenset({
    "MISSING_WORKER_RATE", "RATE_SOURCE_UNKNOWN", "WHOLE_SESSION_RATE",
    "CANDIDATE_ESTIMATE_CLOSED", "WORKER_STATE_NOT_CLEAR",
    "WORKER_LIMITS_UNCONFIRMED", "WORKER_LIMITS_SOURCE_UNKNOWN",
    "WORKER_TAG_FORBIDDEN", "WORKER_LOAD_EXCEEDED", "WORKER_LOAD_LIMIT_UNKNOWN",
})


class RejectionLedger:
    """按 (任务, 原因码, 归因层级, 人, 时段) 聚合拒绝，并给出不越界的解释。"""

    def __init__(self):
        # key -> 聚合行
        self._rows: dict = {}

    def _binding(self, code, worker_id, window_start, scope):
        if scope in (SCOPE_TASK_STRUCTURAL, SCOPE_WORKER_LEVEL, SCOPE_CANDIDATE_LOCAL):
            return scope
        if code in FORCE_TASK_CODES:
            return SCOPE_TASK_STRUCTURAL
        if code in FORCE_WORKER_CODES:
            return SCOPE_WORKER_LEVEL
        if worker_id is None and window_start is None:
            return SCOPE_TASK_STRUCTURAL
        if window_start is None:
            return SCOPE_WORKER_LEVEL
        return SCOPE_CANDIDATE_LOCAL

    def record(self, task_id, code, message, worker_id=None, window_start=None,
               window_end=None, field=None, scope=None):
        binding = self._binding(code, worker_id, window_start, scope)
        wid = worker_id if binding in (SCOPE_WORKER_LEVEL, SCOPE_CANDIDATE_LOCAL) else None
        wstart = window_start if binding == SCOPE_CANDIDATE_LOCAL else None
        wend = window_end if binding == SCOPE_CANDIDATE_LOCAL else None
        key = (task_id, code, binding, wid, wstart)
        row = self._rows.get(key)
        if row is None:
            row = {
                "task_id": task_id, "code": code, "message": message,
                "field": field or FIELD_BY_CODE.get(code, "unmapped:" + str(code)),
                "field_known": bool(field) or code in FIELD_BY_CODE,
                "scope": binding, "worker_id": wid,
                "window_start": wstart, "window_end": wend,
                "candidate_rejection_count": 0,
            }
            self._rows[key] = row
        row["candidate_rejection_count"] += 1
        return row

    def rows_for(self, task_id):
        return [dict(row) for row in self._rows.values() if row["task_id"] == task_id]

    def scope_by_code(self, task_id):
        """code -> 聚合层级；同时出现多层级时取最高（结构性优先）。"""
        rank = {SCOPE_TASK_STRUCTURAL: 2, SCOPE_WORKER_LEVEL: 1, SCOPE_CANDIDATE_LOCAL: 0}
        result: dict = {}
        for row in self._rows.values():
            if row["task_id"] != task_id:
                continue
            current = result.get(row["code"])
            if current is None or rank[row["scope"]] > rank[current]:
                result[row["code"]] = row["scope"]
        return result

    def attribution(self, task_id):
        """给出单个任务的失败归因，局部候选失败不升级为全局原因。"""
        rows = self.rows_for(task_id)
        structural = sorted((r for r in rows if r["scope"] == SCOPE_TASK_STRUCTURAL),
                            key=lambda r: r["code"])
        worker_level = sorted((r for r in rows if r["scope"] == SCOPE_WORKER_LEVEL),
                              key=lambda r: (r["code"], r["worker_id"] or ""))
        local = sorted((r for r in rows if r["scope"] == SCOPE_CANDIDATE_LOCAL),
                       key=lambda r: (r["code"], r["worker_id"] or "", r["window_start"] or ""))
        global_cause = None
        if structural:
            first = structural[0]
            global_cause = {"code": first["code"], "message": first["message"], "field": first["field"]}
        workers = {r["worker_id"] for r in local if r["worker_id"]}
        windows = {r["window_start"] for r in local if r["window_start"]}
        return {
            "task_id": task_id,
            "task_structural": structural,
            "worker_level": worker_level,
            "candidate_local": local,
            "global_cause": global_cause,
            "is_global_cause_claim": global_cause is not None,
            "local_failure_summary": {
                "distinct_workers": len(workers),
                "distinct_windows": len(windows),
                "candidate_local_rejections": sum(r["candidate_rejection_count"] for r in local),
                "total_rejections": sum(r["candidate_rejection_count"] for r in rows),
            },
            "note": ("global_cause 仅在有与具体人/时段无关的结构性原因时给出；"
                     "candidate_local 与 worker_level 只是个别候选或某个人的失败，"
                     "不等同任务全局无解，也不据此断言任何方案都无法完成。"),
        }
