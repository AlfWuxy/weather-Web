"""把可信 CommunityService 回调绑定到已认证账户；不初始化、认证或联网。"""

from copy import deepcopy
from datetime import datetime, timezone
import re

from integration.account_repository import AccountRepository, AccountRepositoryError, _validated
from integration.proposed_weather_web.agriculture_bridge import Principal
from integration.weather_web_adapter import adapt_agriculture_plan
from yilao_agri.community_prediction import freeze_prediction, prediction_sha256, preview_prediction
from yilao_agri.community_service import invalidate
from yilao_agri.community_store import CommunityError, empty_state, now_iso
from yilao_agri.models import InputError, parse_time


REASONS = frozenset({"edit", "load_demo", "weather", "weather_import", "plan", "feedback",
                     "freeze_prediction", "adopt_rate_review", "import"})
PLAN_METADATA = frozenset({"stale", "stale_reason", "displayable"})
MUTABLE_FIELDS = {
    "edit": {"settings", "profile", "plots", "tasks", "helpers", "resources", "policy", "plans"},
    "weather": {"weather", "plans"}, "weather_import": {"weather", "plans"},
    "plan": {"plans"}, "feedback": {"feedback", "plans"},
    "freeze_prediction": {"prediction_journal", "plans"},
    "adopt_rate_review": {"tasks", "plans"},
}


def _fail(message, code, status=400):
    raise CommunityError(message, code=code, status=status)


def _without(row, keys):
    return {key: value for key, value in row.items() if key not in keys}


def _old_plan_preserved(before, after):
    return before == after or (
        _without(before, PLAN_METADATA) == _without(after, PLAN_METADATA)
        and after.get("stale") is True and after.get("displayable") is False
        and isinstance(after.get("stale_reason"), str)
    )


def _plans(old, candidate, reason):
    before, after = old["plans"], candidate["plans"]
    if reason == "import":
        if any(p.get("stale") is not True or p.get("displayable") is not False for p in after):
            _fail("导入计划必须标为历史失效", "IMPORTED_PLAN_NOT_STALE")
        return
    history = before[-199:] if reason == "plan" else before
    previous = after[:-1] if reason == "plan" else after
    if ((reason == "plan" and len(after) != len(history) + 1)
            or len(previous) != len(history)
            or any(not _old_plan_preserved(a, b) for a, b in zip(history, previous))):
        _fail("计划历史不能改写或删除，新计划须经过复核", "PLAN_HISTORY_IMMUTABLE")


def _freeze(old, candidate, started, finished):
    before, after = old["prediction_journal"], candidate["prediction_journal"]
    if len(after) != len(before) + 1 or after[:-1] != before:
        _fail("冻结只允许追加一个对象，旧预测不可改写", "PREDICTION_HISTORY_IMMUTABLE")
    row = after[-1]
    try:
        frozen_at = parse_time(row["frozen_at"])
        if not started <= frozen_at <= finished:
            raise ValueError("冻结时间不在服务端事务回调内")
        preview = preview_prediction(old, row["task_id"], row["worker_id"], row["target_quantity"])
        expected = freeze_prediction(old, {
            "revision": old["revision"], "mode": old["mode"], "task_id": row["task_id"],
            "worker_id": row["worker_id"], "quantity": row["target_quantity"],
            "expected_start_at": row["expected_start_at"], "point_policy": row["point_policy"],
            "confirmed": row["confirmed"], "preview_sha256": preview["preview_sha256"],
        }, now=frozen_at)
        # 原输入单位的呈现已不在冻结对象中；当前目标/情境仍重算，原预览摘要由服务核对。
        digest = row.get("preview_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("预览摘要无效")
        expected["preview_sha256"] = digest
        expected.pop("prediction_id")
        expected.pop("sha256")
        expected["prediction_id"] = "prediction-" + prediction_sha256(expected)[:24]
        expected["sha256"] = prediction_sha256(expected)
        if row != expected:
            raise ValueError("冻结内容与当前资料不符")
    except (InputError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CommunityError("冻结对象未通过当前资料、内容摘要及服务端时刻核对",
                             code="PREDICTION_FREEZE_INVALID") from exc


def _histories(old, candidate, reason):
    journal, incoming = old["prediction_journal"], candidate["prediction_journal"]
    if reason == "import":
        if incoming[:len(journal)] != journal:
            _fail("恢复资料不能改写已留存的预测", "PREDICTION_HISTORY_IMMUTABLE")
        for row in incoming[len(journal):]:
            if row.get("trust_status") != "imported_unverified" or "imported_at" not in row:
                _fail("外来预测必须保留未核实导入来源", "IMPORTED_PREDICTION_TRUST_INVALID")
        for task in candidate["tasks"]:
            for entry in task.get("rate_history", []):
                if (entry.get("source_class") != "imported_unverified_self_report"
                        or entry.get("field_verified") is not False or entry.get("calibrated") is not False
                        or entry.get("confirmed_by_user") is not False or entry.get("n_real") != 0
                        or entry.get("claim_status") != "unverified_import" or "imported_at" not in entry):
                    _fail("导入估时历史不得认证校准或本人采用", "IMPORTED_RATE_HISTORY_TRUST_INVALID")
    elif reason != "freeze_prediction" and incoming != journal:
        _fail("普通操作不能更改预测日志", "PREDICTION_HISTORY_IMMUTABLE")

    history, events = old["feedback"], candidate["feedback"]
    retained = reason != "import" or bool(journal)
    if retained and events[:len(history)] != history:
        _fail("已见反馈不能改写或删除，请追加更正", "FEEDBACK_HISTORY_IMMUTABLE")
    if reason not in {"feedback", "import"} and events != history:
        _fail("反馈只能从反馈入口追加", "FEEDBACK_HISTORY_IMMUTABLE")
    if reason == "feedback" and len(events) != len(history) + 1:
        _fail("每次反馈只允许追加一条", "FEEDBACK_APPEND_REQUIRED")
    additions = events[len(history):] if retained else events
    by_id = {event["event_id"]: event for event in history} if retained else {}
    predictions = {row["prediction_id"]: row for row in incoming}
    for event in additions:
        if reason == "import" and ("imported_at" not in event
                or event.get("verification_status") != "unverified"
                or (candidate["mode"] == "real" and event.get("evidence_type") != "IMPORTED_SELF_REPORT")):
            _fail("外来反馈须保留未核实导入来源", "IMPORTED_FEEDBACK_TRUST_INVALID")
        original = by_id.get(event.get("supersedes_event_id"))
        if original and ("imported_at" in original or original.get("evidence_type") == "IMPORTED_SELF_REPORT"):
            if (event.get("evidence_type") != "IMPORTED_SELF_REPORT" or "imported_at" not in event
                    or (reason == "feedback" and event.get("imported_at") != original.get("imported_at"))
                    or event.get("verification_status") != "unverified"):
                _fail("更正不能洗掉原记录导入来源", "IMPORTED_FEEDBACK_TRUST_INVALID")
        ident = event.get("prediction_id")
        if ident is not None:
            prediction = predictions.get(ident)
            if (prediction is None or prediction.get("task_id") != event["task_id"]
                    or prediction.get("worker_id") != event["worker_id"]):
                _fail("反馈须关联同一农活与人员的既存预测", "PREDICTION_BINDING_INVALID")
        by_id[event["event_id"]] = event

    if reason != "import":
        before = {task["id"]: task for task in old["tasks"]}
        after = {task["id"]: task for task in candidate["tasks"]}
        workers_before = {w["id"] for w in [old["profile"], *old["helpers"]]}
        workers_after = {w["id"] for w in [candidate["profile"], *candidate["helpers"]]}
        for row in journal:
            tid, wid = row.get("task_id"), row.get("worker_id")
            if ((isinstance(tid, str) and tid in before and tid not in after)
                    or (isinstance(wid, str) and wid in workers_before and wid not in workers_after)):
                _fail("已留存预测引用的农活和人员需要保留", "PREDICTION_REFERENCE_IMMUTABLE")
        for event in history:
            tid = event["task_id"]
            if tid not in after or any(after[tid].get(k) != before[tid].get(k)
                                      for k in ("remaining_quantity", "plot_id", "operation")):
                _fail("已有反馈的任务须保留计量基数和引用", "FEEDBACK_TASK_IMMUTABLE")
        for tid, task in after.items():
            prior = before.get(tid, {}).get("rate_history", [])
            records = task.get("rate_history", [])
            if (reason != "adopt_rate_review" and records != prior) or records[:len(prior)] != prior:
                _fail("个人估时采用历史不可改写", "RATE_HISTORY_IMMUTABLE")


class AccountCommunityStore:
    """CommunityService.store兼容接口；callback必须来自服务端代码，不能反序列化HTTP操作。"""

    def __init__(self, repository: AccountRepository, principal: Principal):
        if not isinstance(repository, AccountRepository):
            _fail("需要显式创建的账户仓库", "REPOSITORY_REQUIRED")
        self._repository = repository
        try:
            # 构造即检查身份；两个模式各自绑定同一个不可变服务器身份。
            self._owned = {mode: repository.for_principal(principal, mode=mode)
                           for mode in ("real", "demonstration")}
        except AccountRepositoryError as exc:
            raise CommunityError(str(exc), code=exc.code, status=exc.status) from None

    def _scope(self, mode):
        if not isinstance(mode, str) or mode not in self._owned:
            _fail("资料模式无效", "MODE_CONFLICT")
        return self._owned[mode]

    @staticmethod
    def _active(record):
        if record is not None and record.deleted:
            _fail("此账户资料已删除，须通过明确恢复流程重建", "ACCOUNT_DELETED", 409)

    def read(self, mode="real"):
        owned = self._scope(mode)
        try:
            record = owned.read()
            self._active(record)
            return deepcopy(record.state) if record is not None else empty_state(mode)
        except AccountRepositoryError as exc:
            raise CommunityError(str(exc), code=exc.code, status=exc.status) from None

    def change(self, mode, revision, reason, change):
        owned = self._scope(mode)
        if not isinstance(reason, str) or reason not in REASONS or not callable(change):
            _fail("只接受已列服务端事务操作", "TRUSTED_CHANGE_REQUIRED")
        if reason == "load_demo" and mode != "demonstration":
            _fail("演示重置不能用于真实资料", "MODE_CONFLICT")
        try:
            with self._repository._connection(write=True) as db:
                record = owned._read(db)
                self._active(record)
                old = record.state if record else empty_state(mode)
                if type(revision) is not int or revision != old["revision"]:
                    _fail("资料已更新，请重新读取后保存", "REVISION_CONFLICT", 409)
                started = datetime.now(timezone.utc)
                candidate = _validated(change(deepcopy(old)), mode)
                finished = datetime.now(timezone.utc)
                if reason not in {"load_demo", "import"}:
                    allowed = MUTABLE_FIELDS[reason]
                    if _without(candidate, allowed) != _without(old, allowed):
                        _fail("本操作修改了不属于该入口的资料", "CHANGE_SCOPE_INVALID")
                if reason != "load_demo":
                    _plans(old, candidate, reason)
                    _histories(old, candidate, reason)
                    if reason == "freeze_prediction":
                        _freeze(old, candidate, started, finished)
                elif candidate["plans"] or candidate["feedback"] or candidate["prediction_journal"]:
                    _fail("演示重置不能带入历史安排或真实反馈", "DEMO_HISTORY_INVALID")
                candidate["revision"], candidate["updated_at"] = revision + 1, now_iso()
                if reason == "plan":
                    view = adapt_agriculture_plan(candidate, candidate["plans"][-1], now=finished.isoformat())
                    if view.get("displayable") is not True:
                        _fail("计划重新复核未通过，未保存", "PLAN_REVALIDATION_FAILED", 422)
                else:
                    invalidate(candidate, "账户资料或留存记录已更新，请重新安排")
                result = owned._write(db, candidate, (record.repository_revision if record else 0) + 1)
                return deepcopy(result.state)
        except AccountRepositoryError as exc:
            raise CommunityError(str(exc), code=exc.code, status=exc.status) from None
