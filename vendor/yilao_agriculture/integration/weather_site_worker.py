"""宜老农业独立天气工作进程；不在网页请求中联网，不迁移原站数据库。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import signal
import sys
import threading

from integration.account_repository import AccountRepository, AccountRepositoryError
from integration.community_account_store import AccountCommunityStore
from integration.proposed_weather_web.agriculture_bridge import Principal
from integration.weather_jobs import WeatherJobRepository, WeatherJobError, weather_job_path
from integration.weather_site_registration import make_site_alert_fetcher, principal_for_site_user
from yilao_agri.community_service import CommunityService
from yilao_agri.community_store import CommunityError


class JobDeadline(BaseException):
    """不被取数器的降级捕获吞掉的整项处理时限。"""


def private_directory(path):
    """目录只供农业使用；不依赖外部umask或预警采集成功才收紧权限。"""
    if path != path.resolve():
        raise ValueError("归档目录不能包含符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path != path.resolve():
        raise ValueError("归档目录不能包含符号链接")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


@contextmanager
def job_deadline(seconds):
    # 仅用于专用主线程进程；不修改网页线程或已有定时器。
    if (type(seconds) is not int or not 1 <= seconds <= 90
            or threading.current_thread() is not threading.main_thread()
            or not hasattr(signal, "setitimer")
            or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0)):
        raise ValueError("天气工作进程需要独立主线程与空闲定时器")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise JobDeadline()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class GuardedStore:
    def __init__(self, store, jobs, job, validate_owner, owner_commit=None):
        self.store, self.jobs, self.job, self.validate_owner = store, jobs, job, validate_owner
        self.owner_commit = owner_commit

    def read(self, mode):
        return self.store.read(mode)

    def change(self, mode, revision, reason, transform):
        def commit():
            if self.owner_commit is not None:
                return self.owner_commit(self.job["owner_subject"],
                    lambda: self.store.change(mode, revision, reason, transform))
            if self.validate_owner(self.job["owner_subject"]) is not True:
                raise CommunityError("原账户已失效，请重新登录", code="WEATHER_JOB_ACCOUNT_CHANGED", status=409)
            return self.store.change(mode, revision, reason, transform)

        # 持队列锁只做本地版本核对和保存；所有网络采集已经结束。
        result = self.jobs.commit_if_current(self.job["id"], self.job["lease_token"], commit)
        if result is False:
            raise CommunityError("天气任务已失效，未写入本次结果", code="WEATHER_JOB_LEASE_EXPIRED", status=409)
        return result


def run_one(*, jobs, repository, archive_root, validate_owner, alert_fetcher, deadline_seconds=90,
            owner_commit=None):
    """处理至多一项；失败不重排。返回False表示无待处理任务。"""
    from flask import has_request_context
    if has_request_context():
        raise RuntimeError("天气工作进程不能从HTTP请求调用")
    if (not callable(validate_owner) or not callable(alert_fetcher)
            or (owner_commit is not None and not callable(owner_commit))):
        raise ValueError("须明确提供后台身份核对与预警取数器")
    if jobs.lease_seconds <= deadline_seconds + 10:
        raise ValueError("天气处理时限须小于队列租约并留有提交时间")
    archive = Path(archive_root)
    if not archive.is_absolute() or archive != archive.resolve():
        raise ValueError("归档目录须为绝对规范路径")
    job = jobs.claim()
    if job is None:
        return False
    code, message = "WEATHER_JOB_FAILED", "天气更新未完成，请重新读取资料后再试"
    try:
        with job_deadline(deadline_seconds):
            if validate_owner(job["owner_subject"]) is not True:
                raise CommunityError("原账户已失效", code="WEATHER_JOB_ACCOUNT_CHANGED", status=409)
            owner_archive = archive / sha256(job["owner_subject"].encode()).hexdigest()
            if owner_archive != owner_archive.resolve():
                raise CommunityError("私有归档路径无法确认", code="ARCHIVE_PATH_INVALID", status=503)
            principal = Principal(job["owner_subject"], True, False)
            store = GuardedStore(AccountCommunityStore(repository, principal), jobs, job, validate_owner, owner_commit)
            service = CommunityService(store=store, archive_dir=owner_archive, alert_fetcher=alert_fetcher)
            payload = {"mode": "real", "plot_id": job["plot_id"], "revision": job["revision"]}
            service.validate_weather_refresh(payload)
            private_directory(archive)
            private_directory(owner_archive)
            service.refresh_weather(payload)
        return True
    except JobDeadline:
        code, message = "WEATHER_JOB_TIMED_OUT", "天气处理超时，结果未确认；请先重新读取资料"
    except (CommunityError, AccountRepositoryError) as exc:
        # 只输出预先定义的说明，不保存异常原文、位置、凭据或健康资料。
        if exc.code == "REVISION_CONFLICT":
            code, message = "REVISION_CONFLICT", "资料已修改，本次天气结果未覆盖新资料；请重新读取"
        elif exc.code == "WEATHER_JOB_ACCOUNT_CHANGED":
            code, message = exc.code, "原账户已失效，本次更新已停止；请重新登录"
        elif exc.code == "WEATHER_JOB_LEASE_EXPIRED":
            code, message = exc.code, "天气任务已超时；请先重新读取资料确认结果"
    except Exception:
        # 两个独立数据库无法保证跨库原子提交；异常后绝不自动重试网络或覆盖资料。
        code, message = "WEATHER_JOB_RESULT_UNCONFIRMED", "天气结果尚未确认；请重新读取资料，没有自动重试"
    jobs.finish(job["id"], job["lease_token"], "failed", code=code, message=message)
    return True


def configured_paths(app):
    result = {}
    for name in ("SOURCE_ROOT", "ACCOUNT_DB", "ARCHIVE_ROOT"):
        value = app.config.get("YILAO_AGRICULTURE_" + name)
        if not isinstance(value, str) or not value:
            raise ValueError("农业路径配置不完整")
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("农业路径须为显式绝对规范路径")
        result[name] = path
    if result["SOURCE_ROOT"] != Path(__file__).resolve().parents[1]:
        raise ValueError("工作进程源码与配置不一致")
    # 拒绝把农业独立仓库指向原站库；不读取原站数据，也不执行原站迁移。
    from sqlalchemy.engine import make_url
    url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        original = Path(url.database)
        if not original.is_absolute():
            original = Path(app.instance_path) / original
        if original.resolve() in {result["ACCOUNT_DB"], weather_job_path(result["ACCOUNT_DB"])}:
            raise ValueError("农业账户和任务仓库必须独立于原站数据库")
    return result


def site_owner_validator(subject):
    from core.db_models import User
    from core.extensions import db
    match = re.fullmatch(r"weather-web:user:([1-9][0-9]*):[a-f0-9]{24}", subject)
    if match is None:
        return False
    try:
        # 每次重新读取数据库，避免长运行工作进程保留已删除账户的会话缓存。
        db.session.remove()
        user = db.session.get(User, int(match[1]), populate_existing=True)
        principal = principal_for_site_user(user)
        return bool(principal and principal.guest is False and principal.subject == subject)
    finally:
        db.session.remove()


def site_owner_commit(subject, operation):
    """以原站注销锁保护最后一次账户核对和农业CAS；网络始终在锁外。"""
    from core.db_models import User
    from core.extensions import db
    match = re.fullmatch(r"weather-web:user:([1-9][0-9]*):[a-f0-9]{24}", subject)
    if match is None:
        raise CommunityError("原账户已失效", code="WEATHER_JOB_ACCOUNT_CHANGED", status=409)

    def commit_for(user):
        principal = principal_for_site_user(user)
        if not principal or principal.guest is not False or principal.subject != subject:
            raise CommunityError("原账户已失效", code="WEATHER_JOB_ACCOUNT_CHANGED", status=409)
        return operation()

    if hasattr(User, "deleted_at"):
        # 有注销墓碑的新宿主必须提供其正式守卫；缺依赖时停止，不能降级绕过。
        from services.user.owner_write_guard import OwnerInactiveError, owner_write_guard
        try:
            with owner_write_guard(int(match[1])) as user:
                return commit_for(user)
        except OwnerInactiveError:
            raise CommunityError("原账户已失效", code="WEATHER_JOB_ACCOUNT_CHANGED", status=409) from None
        finally:
            db.session.remove()
    # 老版本main没有注销模块，仍以原站DB行锁防止删号/重用ID穿过最终CAS。
    db.session.rollback()
    try:
        if db.engine.dialect.name == "sqlite":
            result = db.session.execute(db.update(User).where(User.id == int(match[1])).values(id=User.id))
            if result.rowcount != 1:
                return commit_for(None)
        query = db.select(User).where(User.id == int(match[1]))
        if db.engine.dialect.name != "sqlite":
            query = query.with_for_update()
        user = db.session.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
        return commit_for(user)
    finally:
        # 无原站业务字段改动；只释放行锁，不提交no-op更新。
        db.session.rollback()
        db.session.remove()


def validate_site_worker(app):
    """正式注销守卫的依赖须在领取任务和外部取数前就绪。"""
    from core.db_models import User
    if not hasattr(User, "deleted_at"):
        return
    from services.user.owner_write_guard import owner_write_guard
    if not callable(owner_write_guard):
        raise ValueError("原站账户写入守卫未就绪")
    value = str(app.config.get("DISPATCH_LOCK_PATH") or "").strip()
    if not value and (app.testing or app.debug):
        value = str(Path(app.instance_path) / "case-weather-dispatch.lock")
    path = Path(value) if value else None
    if path is None or not path.is_absolute() or path == Path("/"):
        raise ValueError("原站账户锁目录尚未配置")
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    os.close(fd)
    if not os.access(path.parent, os.W_OK | os.X_OK):
        raise ValueError("工作进程无法使用原站账户锁目录")


def main(argv=None):
    parser = argparse.ArgumentParser(description="宜老农业独立天气队列；不修改原站表")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--initialize", action="store_true", help="显式创建独立账户与任务库")
    group.add_argument("--once", action="store_true", help="处理一项后退出，不自动重试")
    group.add_argument("--serve", action="store_true", help="专用后台进程持续处理队列")
    args = parser.parse_args(argv)
    from core.app import create_app
    app = create_app(register_blueprints=False)
    paths = configured_paths(app)
    repository = AccountRepository(paths["ACCOUNT_DB"])
    jobs = WeatherJobRepository(weather_job_path(paths["ACCOUNT_DB"]))
    if args.initialize:
        paths["ACCOUNT_DB"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        repository.initialize()
        jobs.initialize()
        private_directory(paths["ARCHIVE_ROOT"])
        print("AGRICULTURE_STORAGE_INITIALIZED")
        return 0
    if app.config.get("YILAO_AGRICULTURE_WORKBENCH_ENABLED") is not True:
        raise ValueError("农业工作台未启用，后台不取数")
    validate_site_worker(app)
    stop = threading.Event()
    # SIGTERM仅通知循环停止；当前任务在90秒以内结束，之后正常退出。
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    while not stop.is_set():
        with app.app_context():
            worked = run_one(jobs=jobs, repository=repository, archive_root=paths["ARCHIVE_ROOT"],
                             validate_owner=site_owner_validator, alert_fetcher=make_site_alert_fetcher(app),
                             owner_commit=site_owner_commit)
        if args.once:
            print("AGRICULTURE_JOB_PROCESSED" if worked else "AGRICULTURE_QUEUE_IDLE")
            return 0
        if not worked:
            stop.wait(2)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # 服务日志只保留固定诊断码，详细问题在受控排障中查明。
        print("AGRICULTURE_WORKER_STOPPED_CONFIGURATION_OR_STORAGE_ERROR", file=sys.stderr)
        sys.exit(1)
