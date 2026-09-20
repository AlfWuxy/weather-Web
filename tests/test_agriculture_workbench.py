"""农业宿主契约：真实工厂和密码登录，仅用合成资料、临时库，禁止外呼。"""
from contextlib import closing
from datetime import datetime, timezone
from html.parser import HTMLParser
import importlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor/yilao_agriculture"
API = "/api/v1/agriculture/workbench"
ORIGIN = "https://workbench.example.test"
_ISOLATED_ROOT = None
_GUARD_INSTALLED = False


def _isolation_guard(event, args):
    """约束测试期间的实际连接；不能因外部库吞掉请求异常而漏查。"""
    root = _ISOLATED_ROOT
    if root is None:
        return
    if event == "socket.connect":
        raise PermissionError("农业宿主测试禁止外部连接")
    if event == "sqlite3.connect":
        raw = str(args[0])
        if raw != ":memory:":
            parsed = urlsplit(raw) if raw.startswith("file:") else None
            path = Path(unquote(parsed.path) if parsed else raw).resolve()
            if (parsed and parsed.netloc) or not path.is_relative_to(root):
                raise PermissionError("农业宿主测试只能打开本次临时数据库")
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name == ".env" or path.name.startswith(".env."):
            raise PermissionError("农业宿主测试不读取环境文件")
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} and not path.is_relative_to(root):
            raise PermissionError("农业宿主测试不读取既有数据库")


class Document(HTMLParser):
    def __init__(self, response):
        super().__init__()
        assert response.status_code == 200, response.get_data(as_text=True)[:1000]
        self.meta, self.inputs, self.ids, self.links = {}, {}, [], []
        self.scripts, self.styles, self.forms = [], [], []
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "meta":
            self.meta[attrs.get("name")] = attrs.get("content")
        if tag == "input":
            self.inputs[attrs.get("name")] = attrs.get("value")
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs)
        if tag == "script":
            self.scripts.append(attrs)
        if tag == "style":
            self.styles.append(attrs)
        if tag == "form":
            self.forms.append(attrs)


def json_ok(response):
    assert response.status_code == 200, response.get_data(as_text=True)[:1000]
    return response.get_json()


class Host:
    def __init__(self, app, directory, users):
        self.app, self.directory, self.users = app, directory, users
        self.client = app.test_client()

    def request(self, path, *, client=None, **kwargs):
        result = (client or self.client).open(path, base_url=ORIGIN, **kwargs)
        result.get_data()
        result.close()
        return result

    def api(self, path, headers, *, method="GET", body=None, client=None):
        kwargs = {"headers": headers, "method": method, "client": client}
        if body is not None:
            kwargs["json"] = body
        return self.request(API + path, **kwargs)

    def login(self, user="a", *, client=None):
        form = Document(self.request("/login?next=/agriculture/", client=client))
        result = self.request("/login", client=client, method="POST", data={
            **self.users[user], "csrf_token": form.inputs["csrf_token"], "next": "/agriculture/"})
        assert result.status_code == 302
        assert urlsplit(result.headers["Location"]).path == "/agriculture/"
        page = self.request("/agriculture/", client=client)
        document = Document(page)
        return {"X-CSRF-Token": document.meta["csrf-token"],
                "X-Yilao-Account-Context": document.meta["yilao-account-context"]}, document, page

    def account_rows(self):
        with closing(sqlite3.connect((self.directory / "agriculture.sqlite3").as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute("SELECT owner_subject,mode FROM account_states ORDER BY owner_subject,mode").fetchall()


@pytest.fixture
def build_host(tmp_path, monkeypatch, setup_test_environment):
    """只替换输入环境，不替换工厂、登录、CSRF、存储或排程实现。"""
    global _GUARD_INSTALLED
    if not _GUARD_INSTALLED:
        sys.addaudithook(_isolation_guard)
        _GUARD_INSTALLED = True
    monkeypatch.setattr(sys.modules[__name__], "_ISOLATED_ROOT", tmp_path.resolve())
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr("requests.sessions.Session.request", lambda *args, **kwargs: (
        (_ for _ in ()).throw(PermissionError("农业宿主测试禁止外部请求"))))
    safe = {
        "PATH": os.defpath, "LANG": "en_US.UTF-8", "DEBUG": "1", "DEMO_MODE": "1",
        "SECRET_KEY": secrets.token_hex(32), "PAIR_TOKEN_PEPPER": secrets.token_hex(32),
        "RATE_LIMIT_STORAGE_URI": "memory://", "RATE_LIMIT_LOGIN": "100000 per minute",
        "RATE_LIMITS": "100000 per minute", "QWEATHER_AUTH_MODE": "disabled",
        "QWEATHER_KEY": "", "QWEATHER_JWT_PRIVATE_KEY_PATH": "", "REDIS_URL": "",
        "WEATHER_CACHE_REDIS_URL": "", "SENTRY_DSN": "", "SILICONFLOW_API_KEY": "",
        "AMAP_KEY": "", "AMAP_SECURITY_JS_CODE": "", "WXPUSHER_APP_TOKEN": "",
        "FEATURE_STRUCTURED_LOGS": "0", "FEATURE_AUDIT_LOGS": "0",
        "MPLCONFIGDIR": str(tmp_path / "matplotlib"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "NUMBA_CACHE_DIR": str(tmp_path / "numba"), "TMPDIR": str(tmp_path),
    }
    applications = []
    with patch.dict(os.environ, safe, clear=True):
        def build(*, enabled=True, initialize=True):
            directory = (tmp_path / str(len(applications))).resolve()
            directory.mkdir()
            # 使用真实注销锁实现所需的私有父目录，不替换或绕过守卫。
            dispatch_locks = directory / "dispatch-locks"
            dispatch_locks.mkdir(mode=0o700)
            os.environ["DISPATCH_LOCK_PATH"] = str(dispatch_locks / "case-weather-dispatch.lock")
            os.environ["DATABASE_URI"] = "sqlite:///" + str(directory / "site.sqlite3")
            os.environ["YILAO_AGRICULTURE_ACCOUNT_DB"] = str(directory / "agriculture.sqlite3")
            os.environ["YILAO_AGRICULTURE_ARCHIVE_ROOT"] = str(directory / "weather-archive")
            os.environ["YILAO_AGRICULTURE_SITE_ORIGIN"] = ORIGIN
            if enabled:
                os.environ["YILAO_AGRICULTURE_WORKBENCH_ENABLED"] = "1"
            else:
                os.environ.pop("YILAO_AGRICULTURE_WORKBENCH_ENABLED", None)
            if enabled and initialize:
                monkeypatch.syspath_prepend(str(VENDOR))
                from integration.account_repository import AccountRepository
                from integration.weather_jobs import WeatherJobRepository, weather_job_path
                AccountRepository(directory / "agriculture.sqlite3").initialize()
                # 队列与账户独立，且仅由本测试夹具显式初始化。
                WeatherJobRepository(weather_job_path(directory / "agriculture.sqlite3")).initialize()
            factory = importlib.import_module("core.app")
            assert Path(factory.__file__).resolve() == ROOT / "core/app.py"
            app = factory.create_app()
            app.config.update(TESTING=True)
            applications.append(app)
            from core.extensions import db
            from core.db_models import User
            users = {}
            with app.app_context():
                assert db.engine.url.database == str(directory / "site.sqlite3")
                db.create_all()
                for suffix in ("a", "b"):
                    credentials = {"username": "agriculture_test_" + suffix,
                                   "password": secrets.token_urlsafe(24)}
                    user = User(username=credentials["username"], role="user",
                                created_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
                    user.set_password(credentials["password"])
                    db.session.add(user)
                    users[suffix] = credentials
                db.session.commit()
            return Host(app, directory, users)
        try:
            yield build
        finally:
            from core.extensions import db
            for app in applications:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()


@pytest.fixture
def host(build_host):
    return build_host()


def test_default_off_keeps_login_and_creates_no_agriculture_files(build_host):
    vendor_modules = {key for key in sys.modules if key.split(".")[0] in {"integration", "yilao_agri"}}
    host = build_host(enabled=False)
    assert host.app.config["YILAO_AGRICULTURE_WORKBENCH_ENABLED"] is False
    assert "yilao_agriculture_account_repository" not in host.app.extensions
    assert not (host.directory / "agriculture.sqlite3").exists()
    assert not (host.directory / "weather-archive").exists()
    assert host.request("/agriculture/").status_code == 404
    assert host.request(API + "/state").status_code == 404
    page = Document(host.request("/login"))
    assert not any(link.get("data-nav-key") == "agriculture" for link in page.links)
    assert not any(attrs.get("nonce") for attrs in page.scripts)
    assert vendor_modules == {key for key in sys.modules if key.split(".")[0] in {"integration", "yilao_agri"}}


def test_enabled_requires_explicit_database_initialization(build_host):
    with pytest.raises(ValueError, match="明确初始化"):
        build_host(initialize=False)


def test_password_login_csrf_and_anonymous_private_boundaries(host):
    page = host.request("/agriculture/")
    assert page.status_code == 302
    assert urlsplit(page.headers["Location"]).path == "/login"
    assert host.request(API + "/state").status_code == 401
    assert host.request("/agriculture/js/app.js").status_code == 401
    Document(host.request("/login"))
    assert host.request("/login", method="POST", data=host.users["a"]).status_code == 400
    headers, _, _ = host.login()
    assert json_ok(host.api("/health", headers))["field_validated"] is False
    assert host.account_rows() == []


def test_site_navigation_nonce_and_post_logout_are_preserved(host):
    headers, document, response = host.login()
    assert document.ids.count("agri-app") == 1
    assert document.meta["yilao-api-base"] == API
    assert document.meta["yilao-storage-scope"] == "account"
    assert len([link for link in document.links if link.get("data-nav-key") == "agriculture"]) == 2
    inline = [attrs for attrs in document.scripts if not attrs.get("src")] + document.styles
    assert inline
    for attrs in inline:
        assert attrs.get("nonce")
        assert "'nonce-" + attrs["nonce"] + "'" in response.headers["Content-Security-Policy"]
    logout = [form for form in document.forms if form.get("action") == "/logout"]
    assert len(logout) == 2
    assert all(form.get("method").upper() == "POST" for form in logout)
    assert host.request("/logout").status_code == 405
    for asset in ("styles.css", "js/app.js", "js/context.js"):
        result = host.request("/agriculture/" + asset)
        assert result.status_code == 200
        assert "no-store" in result.headers["Cache-Control"]
    result = host.request("/logout", method="POST", data={"csrf_token": headers["X-CSRF-Token"]})
    assert result.status_code == 302
    assert host.request(API + "/state").status_code == 401


def test_account_and_demo_isolation_with_real_logins(host):
    a, _, _ = host.login()
    state = json_ok(host.api("/state", a))
    state["profile"]["name"] = "账户 A 合成资料"
    saved = json_ok(host.api("/state", a, method="PUT", body=state))
    json_ok(host.api("/demo", a, method="POST", body={"mode": "demonstration"}))
    second = host.app.test_client()
    b, _, _ = host.login("b", client=second)
    other = json_ok(host.api("/state", b, client=second))
    assert other["revision"] == 0
    assert "账户 A 合成资料" not in json.dumps(other, ensure_ascii=False)
    assert json_ok(host.api("/state?mode=demonstration", b, client=second))["tasks"] == []
    assert json_ok(host.api("/state", a)) == saved
    assert len(host.account_rows()) == 2
    assert {mode for _, mode in host.account_rows()} == {"real", "demonstration"}


def test_old_page_cannot_read_or_write_after_login_switch(host):
    old, _, _ = host.login()
    state = json_ok(host.api("/state", old))
    current, _, _ = host.login("b")
    stale = {**current, "X-Yilao-Account-Context": old["X-Yilao-Account-Context"]}
    for response in (host.api("/state", stale), host.api("/state", stale, method="PUT", body=state),
                     host.api("/demo", stale, method="POST", body={"mode": "demonstration"})):
        assert response.status_code == 409
        assert response.json["error"]["code"] == "ACCOUNT_CONTEXT_CHANGED"
    assert host.account_rows() == []


def test_csrf_and_account_context_are_independent_requirements(host):
    headers, _, _ = host.login()
    state = json_ok(host.api("/state", headers))
    assert host.api("/state", {**headers, "X-CSRF-Token": "invalid"}, method="PUT", body=state).status_code == 400
    result = host.api("/state", {"X-CSRF-Token": headers["X-CSRF-Token"]})
    assert result.status_code == 409
    assert result.json["error"]["code"] == "ACCOUNT_CONTEXT_CHANGED"
    assert host.account_rows() == []


def test_identity_override_and_cross_origin_are_rejected(host):
    headers, _, _ = host.login()
    result = host.api("/state?owner_subject=other", headers)
    assert result.status_code == 400
    assert result.json["error"]["code"] == "IDENTITY_OVERRIDE_REJECTED"
    result = host.api("/state", {**headers, "Origin": "https://other.example.test"})
    assert result.status_code == 403
    assert host.account_rows() == []


def test_guest_has_no_agriculture_navigation_or_private_data(host):
    assert host.request("/guest?next=/agriculture/").status_code == 302
    assert host.request(API + "/state").status_code == 403
    page = Document(host.request("/login"))
    assert not any(link.get("data-nav-key") == "agriculture" for link in page.links)
    assert host.request("/agriculture/").status_code == 302
    assert host.account_rows() == []


def test_host_upload_limit_is_injected_and_enforced(host):
    host.app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    headers, page, _ = host.login()
    assert int(page.meta["yilao-body-limit"]) == 1024 * 1024
    state = json_ok(host.api("/state", headers))
    state["profile"]["notes"] = "x" * (1024 * 1024)
    response = host.api("/state", headers, method="PUT", body=state)
    assert response.status_code == 413
    assert host.account_rows() == []


def test_demo_plan_runs_engine_and_independent_check_without_real_records(host):
    headers, _, _ = host.login()
    demo = json_ok(host.api("/demo", headers, method="POST", body={"mode": "demonstration"}))
    result = json_ok(host.api("/plan", headers, method="POST", body={
        "mode": "demonstration", "revision": demo["revision"]}))
    assert result["plan"]["verification"]["engine"]["valid"] is True
    assert result["plan"]["verification"]["independent"]["ok"] is True
    assert result["plan"]["sessions"]
    assert result["state"]["feedback"] == []
    persisted = json_ok(host.api("/state?mode=demonstration", headers))
    assert persisted["plans"][-1]["id"] == result["plan"]["id"]
    assert json_ok(host.api("/state", headers))["tasks"] == []


def test_weather_refresh_queues_then_background_rechecks_user_and_uses_host_budget(host):
    from flask import has_request_context
    from core.db_models import User
    from integration.weather_site_registration import make_site_alert_fetcher, principal_for_site_user
    from integration.weather_site_worker import run_one, site_owner_validator, site_owner_commit
    from yilao_agri.official_alert_fetch import fetch_qweather_alerts
    headers, _, _ = host.login()
    state = json_ok(host.api("/state", headers))
    state["plots"] = [{"id": "test-plot", "name": "合成地块", "region_id": "test",
                       "area": {"value": 500, "unit": "sqm"}, "environment": "outdoor",
                       "latitude": 29.27, "longitude": 116.2, "conditions": {"soil": "unknown"}}]
    saved = json_ok(host.api("/state", headers, method="PUT", body=state))
    host.app.config["QWEATHER_API_BASE"] = "https://test.qweatherapi.com/v7"
    forecast = {"source": "合成测试", "kind": "forecast", "issued_at": None, "environment": "outdoor",
                "alert_status": "not_queried", "warnings": [], "provenance": {}, "records": []}
    jobs = host.app.extensions["yilao_agriculture_weather_jobs"]
    repository = host.app.extensions["yilao_agriculture_account_repository"]
    archive = Path(host.app.config["YILAO_AGRICULTURE_ARCHIVE_ROOT"])
    assert jobs.path != repository.path
    with patch("yilao_agri.community_service.fetch_weather", return_value=forecast) as forecast_fetch, \
            patch("yilao_agri.official_alert_fetch.fetch_qweather_alerts", wraps=fetch_qweather_alerts) as alerts_fetch, \
            patch("services.qweather_auth.is_qweather_configured", return_value=True), \
            patch("services.qweather_auth.get_qweather_request_headers", return_value={
                "Authorization": "Bearer synthetic-test-only"}) as auth, \
            patch("services.qweather_budget.reserve_qweather_request", return_value=False) as budget:
        queued = host.api("/weather/refresh", headers, method="POST", body={
            "mode": "real", "revision": saved["revision"], "plot_id": "test-plot"})
        assert queued.status_code == 202, queued.text
        job = queued.json["job"]
        assert job["status"] == "queued"
        for callback in (forecast_fetch, alerts_fetch, auth, budget):
            callback.assert_not_called()
        assert not archive.exists()
        assert not has_request_context(), "后台不能借用保留的HTTP上下文"
        with host.app.app_context():
            user = User.query.filter_by(username=host.users["a"]["username"]).one()
            subject = principal_for_site_user(user).subject
            assert site_owner_validator(subject) is True
            assert jobs.read_for_owner(subject, job["id"]) is not None
            assert run_one(jobs=jobs, repository=repository, archive_root=archive,
                           validate_owner=site_owner_validator, alert_fetcher=make_site_alert_fetcher(host.app),
                           owner_commit=site_owner_commit) is True
        forecast_fetch.assert_called_once()
        alerts_fetch.assert_called_once()
    auth.assert_called_once_with(config=host.app.config, api_base=host.app.config["QWEATHER_API_BASE"])
    budget.assert_called_once_with("weatheralert_v1_current")
    result = json_ok(host.api("/weather/jobs/" + job["id"], headers))
    assert result["job"]["status"] == "succeeded"
    assert result["state"]["revision"] > saved["revision"]
    weather = result["state"]["weather"]["test-plot"]
    assert weather["alert_acquisition"]["code"] == "ALERT_BUDGET_UNAVAILABLE"
    assert weather["alert_feed"]["coverage_status"] == "not_queried"
    assert weather["alert_feed"]["query"]["latitude"] == 29.27
    assert weather["alert_feed"]["query"]["snapshot_complete"] is False
    assert weather["alert_feed"]["items"] == []
    assert "query_snapshot" not in weather["alert_feed"]
    assert "official_alert_acquisition" not in weather["provenance"]
    assert weather["issued_at"] is None
    assert "synthetic-test-only" not in json.dumps(result)
    assert json_ok(host.api("/state", headers))["weather"]["test-plot"] == weather


def test_weather_owner_commit_rechecks_deleted_or_reused_user_before_operation(host):
    from core.db_models import User
    from core.extensions import db
    from integration.weather_site_registration import principal_for_site_user
    from integration.weather_site_worker import site_owner_commit, site_owner_validator
    from yilao_agri.community_store import CommunityError
    host.login()
    executed = []
    with host.app.app_context():
        user = User.query.filter_by(username=host.users["a"]["username"]).one()
        uid, subject = user.id, principal_for_site_user(user).subject
        assert site_owner_validator(subject) is True
        # 初次身份核对已通过，模拟采集期间发生注销，最终CAS必须再次核对。
        user = db.session.get(User, uid)
        if hasattr(User, "deleted_at"):
            user.deleted_at = datetime(2031, 1, 1, tzinfo=timezone.utc)
        else:
            db.session.delete(user)
        db.session.commit()

        def rejected():
            assert site_owner_validator(subject) is False
            with pytest.raises(CommunityError) as caught:
                site_owner_commit(subject, lambda: executed.append("不得执行"))
            assert caught.value.code == "WEATHER_JOB_ACCOUNT_CHANGED"
            assert executed == []

        rejected()
        if hasattr(User, "deleted_at"):
            db.session.delete(db.session.get(User, uid))
            db.session.commit()
        replacement = User(id=uid, username="IMPLEMENTATION_TEST_REUSED_GUARD", role="user",
                           created_at=datetime(2032, 1, 1, tzinfo=timezone.utc))
        replacement.set_password("IMPLEMENTATION_TEST_REUSED_GUARD_PASSWORD")
        db.session.add(replacement)
        db.session.commit()
        new_subject = principal_for_site_user(replacement).subject
        assert new_subject != subject
        rejected()
        assert site_owner_validator(new_subject) is True
        assert site_owner_commit(new_subject, lambda: "IMPLEMENTATION_TEST_ALLOWED") == "IMPLEMENTATION_TEST_ALLOWED"


def test_private_downloads_use_current_account_and_keep_draft_claims(host):
    headers, _, _ = host.login()
    for endpoint in ("/export", "/field-collection", "/field-template"):
        response = host.api(endpoint, headers)
        assert response.status_code == 200
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert "no-store" in response.headers["Cache-Control"]
        if endpoint == "/field-collection":
            assert response.json["claims"] == {
                "n_real": 0, "field_verified": False, "formal_validation_ready": False}
    assert host.account_rows() == []
