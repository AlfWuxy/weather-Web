"""显式初始化的账户/模式隔离仓库；不自动发现、导入或同步任何本机数据库。"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from integration.proposed_weather_web.agriculture_bridge import OwnerPlanSnapshot, Principal
from integration.weather_web_adapter import adapt_agriculture_plan
from yilao_agri.community_service import invalidate, validate_feedback_archive
from yilao_agri.community_store import CommunityError, MODES, json_text, now_iso, validate_state

SCHEMA = "yilao-account-repository-1"
PREDICTION_TRANSPORT_FIELDS = {"trust_status", "imported_at"}
FEEDBACK_TRANSPORT_FIELDS = {"imported_at", "evidence_type", "verification_status"}


class AccountRepositoryError(ValueError):
    def __init__(self, message, *, code="INVALID_INPUT", status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _error(message, code, status=400):
    return AccountRepositoryError(message, code=code, status=status)


def _subject(principal):
    # Principal只是服务器认证结果的载体；本仓库不会认证客户端自报JSON或用户名。
    if not isinstance(principal, Principal) or principal.authenticated is not True:
        raise _error("需要服务器已验证的登录身份", "AUTHENTICATION_REQUIRED", 401)
    value = principal.subject
    if (principal.guest is not False or not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 200 or value.startswith("guest:") or any(ord(c) < 32 for c in value)):
        raise _error("需要正式账户", "REAL_ACCOUNT_REQUIRED", 403)
    return value


def _mode(mode):
    if not isinstance(mode, str) or mode not in MODES:
        raise _error("资料模式无效", "MODE_CONFLICT")
    return mode


def _revision(value):
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise _error("版本号无效", "REVISION_CONFLICT", 409)
    return value


def _content_without(row, transport_fields):
    return {key: value for key, value in row.items() if key not in transport_fields}


def _inherit_correction_provenance(candidate, old_history):
    """更正可改数量/时间，不能在已见结果后更换预测或洗掉导入来源。"""
    previous_by_id = {event["event_id"]: event for event in old_history}
    for event in candidate["feedback"][len(old_history):]:
        ident, prior = event.get("event_id"), event.get("supersedes_event_id")
        if not isinstance(ident, str) or (prior is not None and not isinstance(prior, str)):
            raise _error("更正记录编号格式不正确", "STATE_INVALID")
        original = previous_by_id.get(prior)
        if original is not None:
            if "prediction_id" in event and event["prediction_id"] != original.get("prediction_id"):
                raise _error("更正须保留原预测关联", "PREDICTION_BINDING_IMMUTABLE")
            if original.get("prediction_id") is not None:
                event["prediction_id"] = original["prediction_id"]
            if "imported_at" in original or original.get("evidence_type") == "IMPORTED_SELF_REPORT":
                event.update(imported_at=original.get("imported_at"), evidence_type="IMPORTED_SELF_REPORT",
                             verification_status="unverified")
        previous_by_id[ident] = event


def _validated(raw, mode, *, check_feedback=True):
    try:
        state = validate_state(raw)
        _revision(state.get("revision"))
        if state["mode"] != mode:
            raise _error("真实与演示资料不能相互迁入", "MODE_CONFLICT")
        plots = {row["id"] for row in state["plots"]}
        resources = {row["id"] for row in state["resources"]}
        if set(state["weather"]) - plots:
            raise _error("天气引用的地块不存在", "MISSING_REFERENCE")
        if any(set(task.get("required_resources", [])) - resources for task in state["tasks"]):
            raise _error("农活引用的资源不存在", "MISSING_REFERENCE")
        if check_feedback:
            validate_feedback_archive(state)
        return state
    except CommunityError as exc:
        raise _error(str(exc), "STATE_INVALID", exc.status) from None


@dataclass(frozen=True)
class AccountRecord:
    owner_subject: str
    mode: str
    repository_revision: int
    deleted: bool
    state: dict | None
    plan: dict | None

    def to_owner_plan_snapshot(self):
        if self.deleted or self.state is None or self.plan is None:
            return None
        return OwnerPlanSnapshot(self.owner_subject, deepcopy(self.state), deepcopy(self.plan))


class AccountRepository:
    def __init__(self, path, *, timeout_seconds=5):
        if not isinstance(path, (str, Path)) or not str(path) or str(path) == ":memory:":
            raise _error("请显式指定独立数据库文件", "EXPLICIT_PATH_REQUIRED")
        self.path = Path(path).expanduser().absolute()
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60:
            raise _error("事务等待时间无效", "INVALID_TIMEOUT")
        self.timeout_seconds = timeout_seconds

    def initialize(self):
        """仅显式调用时创建新库；已有非本仓库文件绝不加表或迁移。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            with self._connection():
                pass
            return
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE account_metadata (schema_version TEXT NOT NULL)")
            db.execute("INSERT INTO account_metadata VALUES (?)", (SCHEMA,))
            db.execute("""CREATE TABLE account_states (
                owner_subject TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('real','demonstration')),
                repository_revision INTEGER NOT NULL CHECK(repository_revision > 0),
                deleted INTEGER NOT NULL CHECK(deleted IN (0,1)), state_json TEXT, plan_json TEXT, state_sha256 TEXT,
                PRIMARY KEY(owner_subject, mode),
                CHECK((deleted=1 AND state_json IS NULL AND plan_json IS NULL AND state_sha256 IS NULL)
                      OR (deleted=0 AND state_json IS NOT NULL AND state_sha256 IS NOT NULL)))""")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _connection(self, *, write=False):
        if not self.path.is_file():
            raise _error("账户仓库尚未显式初始化", "REPOSITORY_NOT_INITIALIZED", 503)
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                 timeout=self.timeout_seconds, isolation_level=None)
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                metadata = db.execute("SELECT schema_version FROM account_metadata").fetchall()
            except sqlite3.DatabaseError:
                raise _error("此文件不是支持的账户仓库", "REPOSITORY_SCHEMA_MISMATCH", 503) from None
            if metadata != [(SCHEMA,)]:
                raise _error("账户仓库版本不符", "REPOSITORY_SCHEMA_MISMATCH", 503)
            yield db
            db.commit()
        except sqlite3.OperationalError as exc:
            if db is not None:
                db.rollback()
            code = "REPOSITORY_BUSY" if "locked" in str(exc).lower() or "busy" in str(exc).lower() else "REPOSITORY_UNAVAILABLE"
            raise _error("账户仓库暂时不可用，请重新读取后重试", code, 409 if code == "REPOSITORY_BUSY" else 503) from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def for_principal(self, principal, *, mode):
        return OwnedRepository(self, principal, mode)


class OwnedRepository:
    """账户和模式由服务器身份绑定；各方法不提供可改写目标账户的参数。"""
    def __init__(self, repository, principal, mode):
        self._repository, self._subject, self._mode = repository, _subject(principal), _mode(mode)

    def _read(self, db):
        row = db.execute("SELECT owner_subject, mode, repository_revision, deleted, state_json, plan_json, state_sha256 "
                         "FROM account_states WHERE owner_subject=? AND mode=?", (self._subject, self._mode)).fetchone()
        if row is None:
            return None
        owner, mode, revision, deleted, state_json, plan_json, digest = row
        if deleted:
            return AccountRecord(owner, mode, revision, True, None, None)
        try:
            if sha256(state_json.encode()).hexdigest() != digest:
                raise ValueError("摘要不匹配")
            state = _validated(json.loads(state_json), self._mode)
            plan = json.loads(plan_json) if plan_json is not None else None
            if plan != (state["plans"][-1] if state["plans"] else None):
                raise ValueError("计划快照不匹配")
        except (ValueError, TypeError, KeyError):
            raise _error("账户资料完整性核对失败", "STORED_SNAPSHOT_INVALID", 503) from None
        return AccountRecord(owner, mode, revision, False, state, plan)

    def read(self):
        with self._repository._connection() as db:
            return self._read(db)

    def read_current_snapshot_for_owner(self, subject):
        # 兼容桥接层需要的签名，但只能读取已绑定账户；A不能传B的ID换目标。
        if subject != self._subject:
            raise _error("不能读取其他账户资料", "OWNER_BINDING_MISMATCH", 403)
        record = self.read()
        return record.to_owner_plan_snapshot() if record else None

    def _check_expected(self, record, expected_revision, *, require_active=False):
        expected = _revision(expected_revision)
        actual = record.repository_revision if record else 0
        if expected != actual:
            raise _error("资料已更新，请重新读取后保存", "REVISION_CONFLICT", 409)
        if require_active and (record is None or record.deleted):
            raise _error("账户尚无可更新资料", "NO_OWNED_STATE", 404)

    def _write(self, db, state, revision):
        state = _validated(state, self._mode)
        body = json_text(state)
        plan = state["plans"][-1] if state["plans"] else None
        db.execute("""INSERT INTO account_states VALUES (?,?,?,0,?,?,?)
                      ON CONFLICT(owner_subject,mode) DO UPDATE SET repository_revision=excluded.repository_revision,
                      deleted=0,state_json=excluded.state_json,plan_json=excluded.plan_json,state_sha256=excluded.state_sha256""",
                   (self._subject, self._mode, revision, body, json_text(plan) if plan is not None else None,
                    sha256(body.encode()).hexdigest()))
        return AccountRecord(self._subject, self._mode, revision, False, state, deepcopy(plan))

    def import_state(self, state, *, expected_revision):
        """显式迁入当前账户；历史计划失效，导入不会证明天气、反馈或来源已认证。"""
        candidate = _validated(state, self._mode)
        original_feedback = deepcopy(candidate["feedback"])
        with self._repository._connection(write=True) as db:
            old = self._read(db)
            self._check_expected(old, expected_revision)
            prior_state = old.state if old and not old.deleted else None
            prior_journal = prior_state.get("prediction_journal", []) if prior_state else []
            journal = deepcopy(prior_journal)
            prior_predictions = {row["prediction_id"]: row for row in prior_journal}
            for prediction in candidate.get("prediction_journal", []):
                previous = prior_predictions.get(prediction["prediction_id"])
                if previous is not None:
                    if _content_without(previous, PREDICTION_TRANSPORT_FIELDS) != _content_without(prediction, PREDICTION_TRANSPORT_FIELDS):
                        raise _error("迁入预测与仓库同编号内容冲突", "PREDICTION_CONFLICT", 409)
                else:
                    # 新条目跨仓库后不能再声称在本仓库事前冻结；保留原内容哈希。
                    prediction.update(trust_status="imported_unverified", imported_at=now_iso())
                    journal.append(prediction)
            candidate["prediction_journal"] = journal
            baseline = old.state["revision"] if old and not old.deleted else 0
            candidate["revision"] = max(candidate["revision"], baseline) + 1
            candidate["updated_at"] = now_iso()
            invalidate(candidate, "账户资料已迁入，请重新安排")
            for event in candidate["feedback"]:
                event["verification_status"] = "unverified"
                event["imported_at"] = now_iso()
                if self._mode == "real":
                    event["evidence_type"] = "IMPORTED_SELF_REPORT"
            if prior_journal:
                feedback = deepcopy(prior_state["feedback"])
                prior_events = {event["event_id"] for event in feedback}
                original_by_id = {event["event_id"]: event for event in original_feedback}
                for previous in feedback:
                    incoming = original_by_id.get(previous["event_id"])
                    if (incoming is not None and _content_without(previous, FEEDBACK_TRANSPORT_FIELDS)
                            != _content_without(incoming, FEEDBACK_TRANSPORT_FIELDS)):
                        raise _error("迁入结果与仓库已有记录冲突，请追加更正", "EVENT_CONFLICT", 409)
                feedback.extend(event for event in candidate["feedback"] if event["event_id"] not in prior_events)
                candidate["feedback"] = feedback
                _inherit_correction_provenance(candidate, prior_state["feedback"])
            return self._write(db, candidate, expected_revision + 1)

    def update(self, *, expected_revision, change):
        """可信服务器事务回调；保留计划和反馈历史，变化会令旧计划失效。"""
        if not callable(change):
            raise _error("更新操作必须是服务器回调", "TRUSTED_CHANGE_REQUIRED")
        with self._repository._connection(write=True) as db:
            old = self._read(db)
            self._check_expected(old, expected_revision, require_active=True)
            # 先检查结构和不可改历史；更正继承原绑定后再做完整反馈校验。
            candidate = _validated(change(deepcopy(old.state)), self._mode, check_feedback=False)
            if candidate["plans"] != old.state["plans"]:
                raise _error("计划须通过重新验算入口保存", "PLAN_WRITE_REQUIRES_REVALIDATION")
            if candidate.get("prediction_journal", []) != old.state.get("prediction_journal", []):
                raise _error("一般更新不能添加、改写或删除预测日志", "PREDICTION_HISTORY_IMMUTABLE")
            history = old.state["feedback"]
            if candidate["feedback"][:len(history)] != history:
                raise _error("历史反馈不能改写或删除，请追加更正", "FEEDBACK_HISTORY_IMMUTABLE")
            _inherit_correction_provenance(candidate, history)
            candidate = _validated(candidate, self._mode)
            old_tasks = {row["id"]: row for row in old.state["tasks"]}
            new_tasks = {row["id"]: row for row in candidate["tasks"]}
            for event in history:
                task_id = event["task_id"]
                if task_id not in new_tasks or any(new_tasks[task_id].get(k) != old_tasks[task_id].get(k)
                                                 for k in ("remaining_quantity", "plot_id", "operation")):
                    raise _error("已有反馈的农活需保留计量基数和引用", "FEEDBACK_TASK_IMMUTABLE")
            candidate["revision"], candidate["updated_at"] = old.state["revision"] + 1, now_iso()
            invalidate(candidate, "账户资料或实际记录已更新，请重新安排")
            return self._write(db, candidate, expected_revision + 1)

    def publish_plan(self, plan, *, expected_revision):
        """仅保存针对当前资料生成且通过真实适配器复核的新计划，不接受自报通过标记。"""
        with self._repository._connection(write=True) as db:
            old = self._read(db)
            self._check_expected(old, expected_revision, require_active=True)
            candidate, incoming = deepcopy(old.state), deepcopy(plan)
            if (not isinstance(incoming, dict) or incoming.get("mode") != self._mode
                    or type(incoming.get("state_revision")) is not int
                    or incoming["state_revision"] != old.state["revision"]):
                raise _error("计划与当前资料版本或模式不符", "PLAN_STATE_CONFLICT", 409)
            invalidate(candidate, "已生成新版本")
            candidate["plans"] = (candidate["plans"] + [incoming])[-200:]
            candidate["revision"], candidate["updated_at"] = old.state["revision"] + 1, now_iso()
            candidate = _validated(candidate, self._mode)
            view = adapt_agriculture_plan(candidate, incoming)
            if not view["displayable"]:
                raise _error("计划重新复核未通过，未保存", "PLAN_REVALIDATION_FAILED", 422)
            return self._write(db, candidate, expected_revision + 1)

    def delete(self, *, expected_revision):
        """清空当前账户/模式正文，保留版本墓碑使删除前的写入请求冲突。"""
        with self._repository._connection(write=True) as db:
            old = self._read(db)
            self._check_expected(old, expected_revision, require_active=True)
            db.execute("UPDATE account_states SET repository_revision=?,deleted=1,state_json=NULL,plan_json=NULL,state_sha256=NULL "
                       "WHERE owner_subject=? AND mode=?", (expected_revision + 1, self._subject, self._mode))
            return AccountRecord(self._subject, self._mode, expected_revision + 1, True, None, None)
