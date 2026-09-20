"""独立持久化天气队列；只存服务器身份与地块/版本，不执行网络或认证。

调用方必须从已认证会话取得 owner_subject，不能转送客户端身份。租约只防止
旧执行器提交，不能保证网络请求恰好一次；过期任务失败，绝不自动重新排队。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import uuid


SCHEMA = "yilao-weather-jobs-1"
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
TERMINAL = frozenset({"succeeded", "failed"})
MESSAGES = {
    "WEATHER_JOB_QUEUED": "已排队，等待更新天气",
    "WEATHER_JOB_RUNNING": "正在更新天气，请稍候",
    "WEATHER_JOB_SUCCEEDED": "天气资料已更新",
    "WEATHER_JOB_FAILED": "天气更新未完成，请核对后再试",
    "WEATHER_JOB_LEASE_EXPIRED": "天气更新处理超时，结果尚未确认；没有自动重试",
}
_COLUMNS = (
    "id", "owner_subject", "mode", "plot_id", "revision", "status", "code", "message",
    "result_revision", "created_at_us", "started_at_us", "finished_at_us",
    "lease_expires_at_us", "lease_token_sha256",
)
_CREATE_JOBS = """CREATE TABLE weather_jobs (
    id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode='real'),
    plot_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0),
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
    code TEXT, message TEXT, result_revision INTEGER,
    created_at_us INTEGER NOT NULL, started_at_us INTEGER, finished_at_us INTEGER,
    lease_expires_at_us INTEGER, lease_token_sha256 TEXT,
    CHECK(result_revision IS NULL OR result_revision>revision),
    CHECK((status='queued' AND started_at_us IS NULL AND finished_at_us IS NULL
           AND lease_expires_at_us IS NULL AND lease_token_sha256 IS NULL)
       OR (status='running' AND started_at_us IS NOT NULL AND finished_at_us IS NULL
           AND lease_expires_at_us IS NOT NULL AND lease_token_sha256 IS NOT NULL)
       OR (status IN ('succeeded','failed') AND finished_at_us IS NOT NULL
           AND lease_expires_at_us IS NULL AND lease_token_sha256 IS NULL)),
    CHECK(status!='succeeded' OR result_revision IS NOT NULL)
)"""
_CREATE_DEDUPE = """CREATE UNIQUE INDEX weather_jobs_pending_unique
    ON weather_jobs(owner_subject,plot_id,revision) WHERE status IN ('queued','running')"""
_CREATE_ORDER = "CREATE INDEX weather_jobs_order ON weather_jobs(status,created_at_us,id)"


class WeatherJobError(ValueError):
    def __init__(self, message, *, code="WEATHER_JOB_INVALID", status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _error(message, code="WEATHER_JOB_INVALID", status=400):
    return WeatherJobError(message, code=code, status=status)


def _path(value):
    if not isinstance(value, (str, Path)) or not str(value):
        raise _error("须明确指定独立天气队列的绝对路径")
    path = Path(value)
    if not path.is_absolute() or path != path.resolve() or not path.name:
        raise _error("天气队列路径须为绝对规范路径，不能包含符号链接")
    return path


def weather_job_path(account_database):
    """只派生同父目录的新文件名，不打开或修改账户库。"""
    account = _path(account_database)
    return account.with_name(account.name + ".weather-jobs.sqlite3")


def _text(value, *, maximum, label):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise _error(label + "格式无效")
    return value


def _owner(value):
    subject = _text(value, maximum=200, label="服务器账户标识")
    if subject.startswith("guest:"):
        raise _error("天气更新须使用正式账户", "REAL_ACCOUNT_REQUIRED", 403)
    return subject


def _revision(value):
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise _error("资料版本号无效", "REVISION_CONFLICT", 409)
    return value


def _identifier(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise _error("天气请求编号无效")
    return value


def _stamp(value):
    return None if value is None else (EPOCH + timedelta(microseconds=value)).isoformat()


def _public(row):
    return {**{key: row[key] for key in ("id", "status", "mode", "plot_id", "revision", "code", "message", "result_revision")},
            **{key: _stamp(row[key + "_us"]) for key in ("created_at", "started_at", "finished_at")}}


class WeatherJobRepository:
    def __init__(self, path, *, clock=None, lease_seconds=300, max_pending_per_owner=3,
                 max_pending_global=100, max_completed=1000, retention_seconds=604800,
                 timeout_seconds=5):
        self.path = _path(path)
        if clock is not None and not callable(clock):
            raise _error("队列时钟须为服务器回调")
        for value, upper in ((lease_seconds, 86400), (max_pending_per_owner, 100000),
                             (max_pending_global, 100000), (max_completed, 100000),
                             (retention_seconds, 31536000)):
            if type(value) is not int or not 1 <= value <= upper:
                raise _error("天气队列容量、保留期或租约配置无效")
        if (type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60):
            raise _error("队列事务等待时间无效")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_seconds = lease_seconds
        self.max_pending_per_owner, self.max_pending_global = max_pending_per_owner, max_pending_global
        self.max_completed, self.retention_seconds = max_completed, retention_seconds
        self.timeout_seconds = timeout_seconds

    def _now(self):
        moment = self.clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise _error("队列时钟须返回带时区的时间", "WEATHER_JOB_CLOCK_INVALID", 503)
        delta = moment.astimezone(UTC) - EPOCH
        return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds

    def _check_schema(self, db):
        try:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            metadata = db.execute("SELECT schema_version FROM weather_job_metadata").fetchall()
            columns = tuple(r[1] for r in db.execute("PRAGMA table_info(weather_jobs)"))
            indexes = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"))
            expected_indexes = {"weather_jobs_pending_unique": _CREATE_DEDUPE, "weather_jobs_order": _CREATE_ORDER}
            valid = (tables == {"weather_job_metadata", "weather_jobs"} and [tuple(r) for r in metadata] == [(SCHEMA,)]
                     and columns == _COLUMNS and indexes == expected_indexes
                     and db.execute("PRAGMA user_version").fetchone()[0] == 1)
        except sqlite3.DatabaseError:
            valid = False
        if not valid:
            raise _error("此文件不是支持的独立天气队列", "WEATHER_JOB_SCHEMA_MISMATCH", 503)

    def initialize(self):
        """仅显式初始化；不向已有账户库、其它SQLite或空文件添加队列表。"""
        if self.path != self.path.resolve():
            raise _error("天气队列路径不能包含符号链接")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            with self._transaction():
                pass
            return
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE weather_job_metadata (schema_version TEXT NOT NULL)")
            db.execute("INSERT INTO weather_job_metadata VALUES (?)", (SCHEMA,))
            db.execute(_CREATE_JOBS)
            db.execute(_CREATE_DEDUPE)
            db.execute(_CREATE_ORDER)
            db.execute("PRAGMA user_version=1")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        if self.path != self.path.resolve() or not self.path.is_file():
            raise _error("天气队列尚未显式初始化或路径已变化", "WEATHER_JOB_NOT_INITIALIZED", 503)
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                 timeout=self.timeout_seconds, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self._check_schema(db)
            yield db
            db.commit()
        except sqlite3.OperationalError as exc:
            if db is not None:
                db.rollback()
            busy = "locked" in str(exc).lower() or "busy" in str(exc).lower()
            raise _error("天气队列暂时不可用，请稍后查看", "WEATHER_JOB_BUSY" if busy else "WEATHER_JOB_UNAVAILABLE",
                         409 if busy else 503) from None
        except sqlite3.DatabaseError:
            if db is not None:
                db.rollback()
            raise _error("天气队列文件或结构无效", "WEATHER_JOB_SCHEMA_MISMATCH", 503) from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _maintenance(self, db, now):
        code = "WEATHER_JOB_LEASE_EXPIRED"
        db.execute("""UPDATE weather_jobs SET status='failed',code=?,message=?,finished_at_us=?,
                   lease_expires_at_us=NULL,lease_token_sha256=NULL
                   WHERE status='running' AND lease_expires_at_us<=?""", (code, MESSAGES[code], now, now))
        self._prune(db, now)

    def _prune(self, db, now):
        cutoff = now - self.retention_seconds * 1000000
        db.execute("DELETE FROM weather_jobs WHERE status IN ('succeeded','failed') AND finished_at_us<?", (cutoff,))
        db.execute("""DELETE FROM weather_jobs WHERE id IN (SELECT id FROM weather_jobs
                   WHERE status IN ('succeeded','failed') ORDER BY finished_at_us DESC,created_at_us DESC,id DESC
                   LIMIT -1 OFFSET ?)""", (self.max_completed,))

    def enqueue(self, owner_subject, plot_id, revision):
        """只合并未结束的同账户/地块/版本；终结后仅显式新请求可以重试。"""
        owner, plot, version = _owner(owner_subject), _text(plot_id, maximum=200, label="地块编号"), _revision(revision)
        with self._transaction() as db:
            now = self._now()
            self._maintenance(db, now)
            row = db.execute("""SELECT * FROM weather_jobs WHERE owner_subject=? AND plot_id=? AND revision=?
                             AND status IN ('queued','running')""", (owner, plot, version)).fetchone()
            if row is not None:
                return _public(row)
            count = db.execute("SELECT count(*) FROM weather_jobs WHERE owner_subject=? AND status IN ('queued','running')", (owner,)).fetchone()[0]
            total = db.execute("SELECT count(*) FROM weather_jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= self.max_pending_per_owner or total >= self.max_pending_global:
                raise _error("待处理天气请求已达上限，请等待已有请求结束", "WEATHER_JOB_QUEUE_FULL", 429)
            ident, code = uuid.uuid4().hex, "WEATHER_JOB_QUEUED"
            db.execute("""INSERT INTO weather_jobs(id,owner_subject,mode,plot_id,revision,status,code,message,created_at_us)
                       VALUES (?,?,'real',?,?,'queued',?,?,?)""", (ident, owner, plot, version, code, MESSAGES[code], now))
            return _public(db.execute("SELECT * FROM weather_jobs WHERE id=?", (ident,)).fetchone())

    def read_for_owner(self, owner_subject, job_id):
        owner, ident = _owner(owner_subject), _identifier(job_id)
        with self._transaction() as db:
            self._maintenance(db, self._now())
            row = db.execute("SELECT * FROM weather_jobs WHERE id=? AND owner_subject=?", (ident, owner)).fetchone()
            return None if row is None else _public(row)

    def claim(self):
        """执行器原子领取一条；网络工作在本事务结束后进行。"""
        with self._transaction() as db:
            now = self._now()
            self._maintenance(db, now)
            row = db.execute("SELECT * FROM weather_jobs WHERE status='queued' ORDER BY created_at_us,id LIMIT 1").fetchone()
            if row is None:
                return None
            if now < row["created_at_us"]:
                raise _error("队列时钟早于请求时间", "WEATHER_JOB_CLOCK_INVALID", 503)
            token, code = secrets.token_hex(32), "WEATHER_JOB_RUNNING"
            expires = now + self.lease_seconds * 1000000
            db.execute("""UPDATE weather_jobs SET status='running',code=?,message=?,started_at_us=?,
                       lease_expires_at_us=?,lease_token_sha256=? WHERE id=?""",
                       (code, MESSAGES[code], now, expires, sha256(token.encode()).hexdigest(), row["id"]))
            current = db.execute("SELECT * FROM weather_jobs WHERE id=?", (row["id"],)).fetchone()
            return {**_public(current), "owner_subject": current["owner_subject"],
                    "payload": {"plot_id": current["plot_id"], "revision": current["revision"]},
                    "lease_token": token, "lease_expires_at": _stamp(expires)}

    def _leased(self, db, ident, token, now):
        row = db.execute("SELECT * FROM weather_jobs WHERE id=?", (ident,)).fetchone()
        if (row is None or row["status"] != "running" or row["lease_expires_at_us"] <= now
                or not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None
                or not hmac.compare_digest(row["lease_token_sha256"], sha256(token.encode()).hexdigest())):
            return None
        if now < row["started_at_us"]:
            raise _error("队列时钟早于领取时间", "WEATHER_JOB_CLOCK_INVALID", 503)
        return row

    def _complete(self, db, row, status, code, message, result_revision, now):
        if result_revision is not None:
            _revision(result_revision)
            if result_revision <= row["revision"]:
                raise _error("天气结果版本须晚于请求版本")
        if status == "succeeded" and result_revision is None:
            raise _error("天气更新成功须记录保存后的资料版本")
        db.execute("""UPDATE weather_jobs SET status=?,code=?,message=?,result_revision=?,finished_at_us=?,
                   lease_expires_at_us=NULL,lease_token_sha256=NULL WHERE id=?""",
                   (status, code, message, result_revision, now, row["id"]))
        self._prune(db, now)

    def finish(self, job_id, lease_token, status, *, code=None, message=None, result_revision=None):
        """仅可信执行器传入固定程序提示；禁止把异常原文、凭据或响应体放入message。"""
        ident = _identifier(job_id)
        if not isinstance(status, str) or status not in TERMINAL:
            raise _error("只能结束为成功或失败")
        code = code if code is not None else "WEATHER_JOB_" + status.upper()
        if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None:
            raise _error("天气结果代码无效")
        message = message if message is not None else MESSAGES["WEATHER_JOB_" + status.upper()]
        _text(message, maximum=500, label="天气结果提示")
        with self._transaction() as db:
            now = self._now()
            self._maintenance(db, now)
            row = self._leased(db, ident, lease_token, now)
            if row is None:
                return False
            self._complete(db, row, status, code, message, result_revision, now)
            return True

    def commit_if_current(self, job_id, lease_token, operation):
        """持有队列写锁且租约有效时调用一次账户CAS，并记录其新版本。

        operation只能做无网络的账户CAS，返回新state字典或整数版本；失效返回False
        且不调用operation。两个SQLite不能原子提交：账户可能已保存但队列未提交，
        此时抛COMMIT_UNCERTAIN，不得自动重复operation或网络请求。租约在进入CAS前
        核查，队列锁持有至结束；执行器还须在外层设置比租约更短的总执行期限。
        """
        ident = _identifier(job_id)
        if not callable(operation):
            raise _error("账户提交须由服务器提供可调用操作")
        operation_returned = False
        try:
            with self._transaction() as db:
                now = self._now()
                self._maintenance(db, now)
                row = self._leased(db, ident, lease_token, now)
                if row is None:
                    return False
                result = operation()
                operation_returned = True
                revision = result.get("revision") if isinstance(result, dict) else result
                code = "WEATHER_JOB_SUCCEEDED"
                self._complete(db, row, "succeeded", code, MESSAGES[code], revision, self._now())
            return result
        except Exception:
            if operation_returned:
                raise _error("资料可能已经保存，但队列结果未确认；请重新读取资料，勿自动重试",
                             "WEATHER_JOB_COMMIT_UNCERTAIN", 503) from None
            raise
