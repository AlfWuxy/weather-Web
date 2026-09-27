"""本机资料存储：真实与演示隔离、版本冲突保护、事务快照。"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import json
import math
import sqlite3
from zoneinfo import ZoneInfo

MODES = {"real", "demonstration"}
SCHEMA = "community-1"


class CommunityError(ValueError):
    def __init__(self, message, *, code="INVALID_INPUT", field=None, status=400):
        super().__init__(message)
        self.code, self.field, self.status = code, field, status


def now_iso():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def empty_state(mode="real"):
    if not isinstance(mode,str) or mode not in MODES:
        raise CommunityError("资料模式无效", field="mode")
    tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).date().isoformat()
    return {"schema_version": SCHEMA, "revision": 0, "mode": mode,
            "settings": {"place": "都昌", "timezone": "Asia/Shanghai", "planning_date": tomorrow,
                         "start_time": "06:00", "end_time": "18:00", "entered_by": "self"},
            "profile": {"id": "elder", "name": "我", "role": "elder_self", "state": "unknown",
                        "state_checked_at": None, "availability": [], "initial_rest_confirmed": False,
                        "used_active_minutes_by_date": {}, "limits": {"review_status": "unknown", "source": "",
                        "max_active_minutes_per_day": None, "max_continuous_active_minutes": None,
                        "min_rest_minutes": None, "max_load_kg": None, "forbidden_tags": []},
                        "limitations_note": "", "limits_valid_until": None},
            "helpers": [], "plots": [], "tasks": [], "resources": [], "weather": {}, "plans": [], "feedback": [], "prediction_journal": [],
            "policy": {"review_status": "unknown", "source": "", "max_forecast_age_minutes": 720,
                       "max_forecast_horizon_hours": 72, "require_daylight": True,
                       "blocked_hazards": ["thunderstorm", "flood"],
                       "required_resource_kinds": ["water", "rest_place"], "weather_limits": {}},
            "updated_at": None}


def finite(value, field, minimum=0, maximum=1e9):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CommunityError("请填写有效数字", field=field)
    if not minimum <= value <= maximum:
        raise CommunityError(f"数字须在 {minimum:g} 至 {maximum:g} 之间", field=field)
    return value


def validate_state(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA or not isinstance(raw.get("mode"),str) or raw.get("mode") not in MODES:
        raise CommunityError("这不是支持的宜老农业资料文件", field="schema_version")
    try:
        encoded = json_text(raw)
    except (ValueError, TypeError, RecursionError):
        raise CommunityError("资料含无法读取的值") from None
    if len(encoded.encode()) > 4_000_000:
        raise CommunityError("资料超过4MB，请分批导出", status=413)
    state = deepcopy(raw)
    # 旧资料没有事前估时页，读取时使用空列表，不伪造历史预测。
    state.setdefault("prediction_journal", [])
    for key, limit in (("plots",20),("helpers",11),("tasks",20),("resources",50),("plans",200),("feedback",5000)):
        rows = state.get(key)
        if not isinstance(rows, list) or len(rows) > limit or any(not isinstance(r,dict) for r in rows):
            raise CommunityError(f"{key} 的数量或格式不正确", field=key)
        if key not in {"plans", "feedback"}:
            ids = [r.get("id") for r in rows]
            if any(not isinstance(i,str) or not i.strip() or len(i)>100 for i in ids) or len(set(ids)) != len(ids):
                raise CommunityError("每条资料需要不同的编号", field=key)
    if not isinstance(state.get("profile"),dict) or not isinstance(state.get("settings"),dict):
        raise CommunityError("缺少本人资料或规划日期")
    if not isinstance(state.get("weather"),dict) or not isinstance(state.get("policy"),dict):
        raise CommunityError("天气与排程设置格式错误")
    journal=state["prediction_journal"]
    if not isinstance(journal,list) or len(journal)>1000 or any(not isinstance(row,dict) for row in journal):
        raise CommunityError("估时留存的数量或格式不正确",field="prediction_journal")
    prediction_ids=[row.get("prediction_id") for row in journal]
    if any(not isinstance(ident,str) or not 1<=len(ident)<=150 for ident in prediction_ids) or len(set(prediction_ids))!=len(prediction_ids):
        raise CommunityError("估时留存编号缺失或重复",field="prediction_journal")
    policy=state["policy"]
    if not isinstance(policy.get("weather_limits",{}),dict):
        raise CommunityError("天气限制格式错误",field="policy.weather_limits")
    for key in ("max_forecast_age_minutes","max_forecast_horizon_hours"):
        finite(policy.get(key),"policy."+key,0,10080)
    if state["settings"].get("timezone") != "Asia/Shanghai":
        raise CommunityError("本社区版使用中国标准时间",field="settings.timezone")
    try:
        datetime.strptime(state["settings"]["planning_date"], "%Y-%m-%d")
        for key in ("start_time","end_time"):
            datetime.strptime(state["settings"][key], "%H:%M")
    except (ValueError, TypeError, KeyError):
        raise CommunityError("请填写有效的规划日期与开始、结束时间",field="settings") from None
    plot_ids={p["id"] for p in state["plots"]}
    workers = [state["profile"],*state["helpers"]]
    ids=[w.get("id") for w in workers]
    if any(not isinstance(i,str) or not i.strip() for i in ids) or len(set(ids))!=len(ids):
        raise CommunityError("本人和帮手的编号不能相同",field="helpers")
    for worker in workers:
        if not isinstance(worker.get("state"),str) or worker.get("state") not in {"clear","stop","unknown"}:
            raise CommunityError("人员当天状态不正确",field="profile.state")
        if not isinstance(worker.get("limits"),dict) or not isinstance(worker.get("used_active_minutes_by_date",{}),dict):
            raise CommunityError("人员限制和活动记录格式不正确",field="profile.limits")
        if not isinstance(worker.get("availability",[]),list):
            raise CommunityError("人员可用时间格式不正确",field="profile.availability")
        if type(worker.get("initial_rest_confirmed",False)) is not bool:
            raise CommunityError("出工前休息确认须为明确的是或否",field="profile.initial_rest_confirmed")
        for key in ("max_active_minutes_per_day","max_continuous_active_minutes","min_rest_minutes","max_load_kg"):
            if worker["limits"].get(key) is not None:
                finite(worker["limits"][key],"profile.limits."+key,0,10080)
        if not isinstance(worker["limits"].get("forbidden_tags",[]),list) or any(not isinstance(tag,str) for tag in worker["limits"].get("forbidden_tags",[])):
            raise CommunityError("禁做动作的格式不正确",field="profile.limits.forbidden_tags")
        if not isinstance(worker.get("limitations_note",""),str):
            raise CommunityError("活动限制备注须为文字",field="profile.limitations_note")
    for p in state["plots"]:
        if p.get("latitude") is not None:
            finite(p["latitude"],"plots.latitude",-90,90)
        if p.get("longitude") is not None:
            finite(p["longitude"],"plots.longitude",-180,180)
        area=p.get("area",{})
        if not isinstance(area,dict):
            raise CommunityError("地块面积格式不正确",field="plots.area")
        if not isinstance(area.get("unit"),str) or area.get("unit") not in {"mu","sqm"}:
            raise CommunityError("地块面积请选择亩或平方米",field="plots.area")
        finite(area.get("value"),"plots.area",0,1e8)
    for resource in state["resources"]:
        if type(resource.get("confirmed",False)) is not bool:
            raise CommunityError("资源可用确认须为明确的是或否",field="resources.confirmed")
        if not isinstance(resource.get("availability",[]),list):
            raise CommunityError("资源可用时间格式不正确",field="resources.availability")
        finite(resource.get("capacity",1),"resources.capacity",1,100)
    task_ids={t["id"] for t in state["tasks"]}
    for t in state["tasks"]:
        if not isinstance(t.get("plot_id"),str) or t.get("plot_id") not in plot_ids:
            raise CommunityError("农活引用的地块不存在；请先处理相关农活",field="tasks.plot_id")
        q=t.get("remaining_quantity",{})
        if not isinstance(q,dict):
            raise CommunityError("剩余工作量格式不正确",field="tasks.remaining_quantity")
        if not isinstance(q.get("unit"),str) or q.get("unit") not in {"mu","sqm","trip","kg","plant","m","m3"}:
            raise CommunityError("农活的计量单位不受支持",field="tasks.remaining_quantity")
        finite(q.get("value"),"tasks.remaining_quantity",0,1e9)
        for key in ("task_code","operation","method"):
            if key in t and not isinstance(t[key],str):
                raise CommunityError("农活种类和做法须为有效条目",field="tasks."+key)
        if q["unit"] in {"trip","plant"} and q["value"] != int(q["value"]):
            raise CommunityError("趟和株必须为整数",field="tasks.remaining_quantity")
        if not isinstance(t.get("rates",{}),dict):
            raise CommunityError("估时资料格式不正确",field="tasks.rates")
        for key in ("session","agronomy","wait_duty"):
            if not isinstance(t.get(key,{}),dict):
                raise CommunityError("农活条件格式不正确",field="tasks."+key)
        for key in ("tags","required_resources"):
            if not isinstance(t.get(key,[]),list) or any(not isinstance(v,str) for v in t.get(key,[])):
                raise CommunityError("农活动作和所需资源须使用明确的条目",field="tasks."+key)
        if not isinstance(t.get("rate_history",[]),list) or any(not isinstance(row,dict) for row in t.get("rate_history",[])):
            raise CommunityError("估时回顾历史格式不正确",field="tasks.rate_history")
        if "wait_duty" in t:
            duty=t["wait_duty"]
            if not isinstance(duty.get("status"),str) or duty.get("status") not in {"known","unknown"}:
                raise CommunityError("请确认等待情况",field="tasks.wait_duty.status")
            if duty["status"]=="known":
                finite(duty.get("minutes"),"tasks.wait_duty.minutes",0,10080)
                if not isinstance(duty.get("reason"),str) or duty.get("reason") not in {"equipment","helper","material","weather","watch","other"}:
                    raise CommunityError("请填写等待的原因；休息和劳动应另计",field="tasks.wait_duty.reason")
        for worker_id,rate in t.get("rates",{}).items():
            if worker_id not in ids or not isinstance(rate,dict):
                raise CommunityError("估时引用的人员不存在",field="tasks.rates")
            low=finite(rate.get("low"),"tasks.rates.low",1e-8,1e7)
            high=finite(rate.get("high"),"tasks.rates.high",1e-8,1e7)
            if high < low:
                raise CommunityError("最长估时不能小于最短估时",field="tasks.rates")
        if not isinstance(t.get("depends_on",[]),list) or any(not isinstance(d,str) or d not in task_ids for d in t.get("depends_on",[])):
            raise CommunityError("前置农活不存在",field="tasks.depends_on")
    return state


class CommunityStore:
    def __init__(self, path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS states (mode TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, mode TEXT, revision INTEGER, reason TEXT, at TEXT, body TEXT)")
            for mode in sorted(MODES):
                db.execute("INSERT OR IGNORE INTO states VALUES (?,?,?)",(mode,0,json_text(empty_state(mode))))
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=15)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def read(self,mode="real"):
        if not isinstance(mode,str) or mode not in MODES:
            raise CommunityError("资料模式无效",field="mode")
        with self.connection() as db:
            state=json.loads(db.execute("SELECT body FROM states WHERE mode=?",(mode,)).fetchone()[0])
            state.setdefault("prediction_journal",[])
            return state

    def change(self,mode,revision,reason,change):
        if not isinstance(mode,str) or mode not in MODES:
            raise CommunityError("资料模式无效",field="mode")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old_rev,body=db.execute("SELECT revision,body FROM states WHERE mode=?",(mode,)).fetchone()
            if type(revision) is not int or revision != old_rev:
                raise CommunityError("资料已在另一处更新，请重新读取后再保存",code="REVISION_CONFLICT",status=409)
            result=change(json.loads(body))
            result["mode"],result["revision"],result["updated_at"]=mode,old_rev+1,now_iso()
            result=validate_state(result)
            db.execute("INSERT INTO snapshots(mode,revision,reason,at,body) VALUES (?,?,?,?,?)",(mode,old_rev,reason,now_iso(),body))
            db.execute("UPDATE states SET revision=?,body=? WHERE mode=?",(old_rev+1,json_text(result),mode))
            return result
