"""把田块、天气、作业记录与经复查的排程连接为社区工作流。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

from .community_store import CommunityError, CommunityStore, empty_state, finite, json_text, now_iso, validate_state
from .models import parse_time, is_placeholder_source, validate_request, alert_query_issues
from .cli import run_checked
from .recalc import check_plan
from .live_weather import fetch_weather
from .community_load import assess_day_load, is_untimed_not_done
from .community_catalog import get_community_catalog

ROOT=Path(__file__).resolve().parents[1]
TZ=ZoneInfo("Asia/Shanghai")
SQM_PER_MU=2000/3
ACTIVITY_TAGS={"bending","squatting","lifting","carrying","prolonged_standing","climbing","powered_machinery","pesticide_exposure"}
ACTIVITY_ALIASES={"弯腰":"bending","蹲姿":"squatting","蹲下":"squatting","搬抬":"lifting","负重搬运":"carrying","久站":"prolonged_standing","登高":"climbing","动力农机":"powered_machinery","接触农药":"pesticide_exposure"}
PERSON_KEYS={"id","role","state","availability","limits","used_active_minutes_by_date","initial_rest_confirmed","worker_profile"}
TASK_KEYS={"id","plot_id","crop_id","stage","operation","method","remaining_quantity","rates","earliest_start","deadline","deadline_source","priority","divisible","min_chunk_minutes","quantity_step","depends_on","required_resources","tags","load_per_trip_kg","session","once","agronomy","crop_alias","parallel_within_task","deadline_basis","operation_scope","wait_duty"}


def select(row,keys):
    return {k:deepcopy(v) for k,v in row.items() if k in keys and v is not None}


WEATHER_REQUEST_KEYS={"source","issued_at","kind","environment","records","grid","alert_feed","exposure","wbgt_method","forecast_run_snapshot"}


def forecast_reference(weather,decision_at,plot=None):
    """固定模型批次从初始化计龄；下载或导入时刻不充当签发。"""
    if "forecast_run_snapshot" in weather:
        from .forecast_run import validate_run_snapshot, validate_run_location
        reference=validate_run_snapshot(weather,decision_at)
        if plot is not None:
            validate_run_location(weather["forecast_run_snapshot"],plot)
        return reference
    return parse_time(weather["issued_at"])


def request_weather(weather):
    clean=select(weather,WEATHER_REQUEST_KEYS)
    if "forecast_run_snapshot" in weather:
        clean["forecast_run_snapshot"]=deepcopy(weather["forecast_run_snapshot"])
        # 新分支要求明确没有签发时间，不能被通用去空值逻辑删掉。
        if "issued_at" in weather:
            clean["issued_at"]=weather["issued_at"]
    return clean


def real_source(value):
    return isinstance(value,str) and not is_placeholder_source(value) and value.strip().casefold() not in {"copied_from_plan","copied_from_schedule"}


def invalidate(state,reason):
    for plan in state["plans"]:
        plan["stale"],plan["stale_reason"],plan["displayable"]=True,reason,False


def canonical_quantity(quantity):
    if quantity is None:
        return None,None
    if not isinstance(quantity,dict):
        raise CommunityError("完成量须包含数字与单位",field="completed_quantity")
    value=finite(quantity.get("value"),"completed_quantity")
    unit=quantity.get("unit")
    if not isinstance(unit,str) or unit not in {"mu","sqm","trip","plant","kg","m","m3"}:
        raise CommunityError("完成量单位错误",field="completed_quantity.unit")
    if unit in {"trip","plant"} and int(value)!=value:
        raise CommunityError("完成的趟数或株数须为整数",field="completed_quantity")
    return (value*SQM_PER_MU,"sqm") if unit=="mu" else (value,unit)


def remaining_for(state,task):
    original,unit=canonical_quantity(task["remaining_quantity"])
    done=0.0
    unknown=0
    for event in current_events(state):
        if event.get("task_id")!=task["id"]:
            continue
        value,event_unit=canonical_quantity(event.get("completed_quantity"))
        if value is None:
            unknown+=1
        elif event_unit!=unit:
            raise CommunityError("历史完成记录单位与任务不一致",field="feedback.completed_quantity")
        else:
            done+=value
    left=original-done
    if left < -1e-7:
        raise CommunityError("完成量超过登记的待做量，请先核对任务与记录",field="feedback.completed_quantity")
    out_unit=task["remaining_quantity"]["unit"]
    divisor=SQM_PER_MU if out_unit=="mu" else 1
    return {"value":max(0,left)/divisor,"unit":out_unit,"reported_completed":done/divisor,"unknown_records":unknown,
            "basis":"self_report" if state["mode"]=="real" else "synthetic"}


def current_events(state):
    superseded={e["supersedes_event_id"] for e in state["feedback"] if e.get("supersedes_event_id")}
    return [e for e in state["feedback"] if e.get("event_id") not in superseded]


def validate_feedback_archive(state):
    """导入与新增使用相同的记录边界；指纹不能替代内容校验。"""
    tasks={t["id"]:t for t in state["tasks"]}
    people={w["id"] for w in [state["profile"],*state["helpers"]]}
    history={}
    replaced=set()
    for event in state["feedback"]:
        ident=event.get("event_id")
        if not isinstance(ident,str) or not 8<=len(ident)<=100 or ident in history:
            raise CommunityError("反馈编号缺失或重复",field="feedback.event_id")
        if event.get("task_id") not in tasks or event.get("worker_id") not in people:
            raise CommunityError("反馈引用的农活或人员不存在",field="feedback")
        if event.get("consent") is not True:
            raise CommunityError("导入记录缺少本地保存同意",field="feedback.consent")
        if event.get("prediction_id") is not None and (not isinstance(event["prediction_id"],str) or not 1<=len(event["prediction_id"])<=150):
            raise CommunityError("关联估时编号格式不正确",field="feedback.prediction_id")
        status=event.get("status")
        if status not in {"completed","partial","not_done","interrupted","unknown"}:
            raise CommunityError("反馈状态无效",field="feedback.status")
        amount,unit=canonical_quantity(event.get("completed_quantity"))
        _,task_unit=canonical_quantity(tasks[event["task_id"]]["remaining_quantity"])
        if status=="unknown":
            if amount is not None: raise CommunityError("未知反馈不得携带完成量",field="feedback.completed_quantity")
        else:
            if amount is None or unit!=task_unit or (status=="not_done" and amount!=0) or (status in {"completed","partial"} and amount<=0):
                raise CommunityError("反馈状态与完成量不一致",field="feedback.completed_quantity")
            for key in ("quantity_source","clock_source"):
                if not real_source(event.get(key)):
                    raise CommunityError("反馈不能照抄安排，需有实际记录来源",field="feedback."+key)
            if not is_untimed_not_done(event):
                try: a,b=parse_time(event["started_at"]),parse_time(event["ended_at"])
                except (KeyError,ValueError,TypeError): raise CommunityError("反馈起止时间格式无效",field="feedback.started_at") from None
                net=finite(event.get("net_minutes"),"feedback.net_minutes",0,1440)
                rest=finite(event.get("rest_minutes",0),"feedback.rest_minutes",0,1440)
                if b<a or b>datetime.now(TZ)+timedelta(minutes=5) or net+rest>(b-a).total_seconds()/60+1e-6 or (amount>0 and net<=0):
                    raise CommunityError("反馈时间与实际劳动分钟不一致",field="feedback.net_minutes")
        prior=event.get("supersedes_event_id")
        if prior:
            original=history.get(prior)
            if not original or prior in replaced or original["task_id"]!=event["task_id"] or original["worker_id"]!=event["worker_id"]:
                raise CommunityError("反馈更正链不正确",field="feedback.supersedes_event_id")
            if original.get("prediction_id")!=event.get("prediction_id"):
                raise CommunityError("更正记录需要保留原来的估时关联",field="feedback.prediction_id")
            replaced.add(prior)
        history[ident]=event
    actual=[e for e in current_events(state) if e["status"] not in {"unknown","not_done"}]
    for i,event in enumerate(actual):
        start,end=parse_time(event["started_at"]),parse_time(event["ended_at"])
        for other in actual[:i]:
            if other["worker_id"]==event["worker_id"] and max(start,parse_time(other["started_at"]))<min(end,parse_time(other["ended_at"])):
                raise CommunityError("同一人的实际作业时间重叠，可能重复提交；若需改动请更正原记录",code="PHYSICAL_EVENT_CONFLICT",field="feedback",status=409)
    for task in tasks.values(): remaining_for(state,task)


def estimates(state):
    rows=[]
    for task in state["tasks"]:
        remain=remaining_for(state,task)
        q,unit=canonical_quantity(remain)
        people=[]
        for person,rate in task.get("rates",{}).items():
            ru=rate.get("unit")
            rq=q/SQM_PER_MU if ru=="mu" and unit=="sqm" else q
            if ("sqm" if ru=="mu" else ru)!=unit or rate.get("scope")!="net_work":
                continue
            extra=sum(v for v in task.get("session",{}).values() if type(v) in (int,float))
            people.append({"worker_id":person,"net_minutes_low":round(rq*rate["low"],2),"net_minutes_high":round(rq*rate["high"],2),
                           "overhead_minutes":extra,"source":rate.get("source",""),"clock_minutes":None})
        rows.append({"task_id":task["id"],"name":task.get("name",task["id"]),"remaining":remain,"workers":people,
                     "warnings":["净劳动估时不含休息；准备、来回和每次出工重复开销须另计。","个人估计尚未形成经实测校准的准确率。"]})
    return {"tasks":rows,"meaning":"工作量估计，不能作为健康安全许可"}


def make_demo():
    state=empty_state("demonstration")
    request=json.loads((ROOT/"examples/duchang_demo.json").read_text(encoding="utf-8"))
    now=datetime.now(TZ)
    planning_day=now.date()
    day_end=datetime.fromisoformat(planning_day.isoformat()+"T"+state["settings"]["end_time"]).replace(tzinfo=TZ)
    # 当日示例时段已经结束时整体移到次日，保留适配器原有的过期检查。
    if now>=day_end: planning_day+=timedelta(days=1)
    origin=parse_time(request["horizon_start"]).astimezone(TZ).date()
    shift=planning_day-origin
    def move(value):
        # 日期键（例如每日累计负荷）与任意相对日的ISO时间须一起移动，来源文字保持原样。
        if isinstance(value,dict): return {move(k):move(v) for k,v in value.items()}
        if isinstance(value,list): return [move(v) for v in value]
        if isinstance(value,str):
            if len(value)==10 and value[4]=="-" and value[7]=="-":
                try: return (datetime.strptime(value,"%Y-%m-%d").date()+shift).isoformat()
                except ValueError: pass
            if len(value)>=19 and value[10]=="T":
                try: return (parse_time(value)+shift).isoformat()
                except (ValueError,TypeError): pass
        return value
    request=move(request)
    state["settings"].update(planning_date=planning_day.isoformat(),place="都昌（虚构示例）")
    state["profile"].update(request["workers"][0],name="示例本人",state_checked_at=request["now"],limits_valid_until=request["horizon_end"])
    state["helpers"]=[dict(request["workers"][1],name="示例帮手",role="helper",state_checked_at=request["now"],limits_valid_until=request["horizon_end"])]
    state["plots"]=[dict(p,name=("北边辣椒地" if i==0 else "南边生菜地"),crop_id=("pepper" if i==0 else "lettuce"),stage="vegetative",crop_identity_status="confirmed",area={"value":3 if i==0 else 1,"unit":"mu"}) for i,p in enumerate(request["plots"])]
    names={"weed_peppers":"辣椒地除草","carry_material":"搬运肥料","fertilize_lettuce":"生菜地施肥","apply_material":"生菜地施肥"}
    codes={"weeding":"weed_hand","transport":"haul_material","fertilization":"topdress"}
    state["tasks"]=[dict(t,name=names.get(t["id"],t["operation"]),task_code=codes.get(t["operation"],t["operation"]),distance_m=50 if t["remaining_quantity"]["unit"]=="trip" else None) for t in request["tasks"]]
    for task in state["tasks"]:
        task["method"]={"hand_hoe":"hoe","handcart":"wheelbarrow","manual_application":"broadcast"}.get(task["method"],task["method"])
    state["resources"]=[dict(r,name={"drinking_water":"饮用水","shaded_rest":"凉爽休息处","hoe":"锄头","handcart":"手推车","fertilizer_scoop":"施肥工具"}.get(r["id"],r["id"]),confirmed=True) for r in request["resources"]]
    state["weather"]={k:dict(v,retrieved_at=v["issued_at"],issue_time_status="synthetic",alert_status="synthetic",location={"latitude":state["plots"][i]["latitude"],"longitude":state["plots"][i]["longitude"]}) for i,(k,v) in enumerate(request["weather"].items())}
    state["policy"]=request["policy"]
    return state


def readiness(state,now=None):
    now=now or datetime.now(TZ)
    items=[]
    def add(code,message,field,severity="blocking"):
        items.append({"code":code,"message":message,"field":field,"severity":severity})
    if not state["plots"]: add("NO_PLOTS","先记下要去的地块","plots")
    if not state["tasks"]: add("NO_TASKS","先记下要做的农活和待做量","tasks")
    if state["mode"]=="demonstration":
        return {"ready":not items,"items":items}
    p=state["policy"]
    if p.get("review_status")!="confirmed" or not real_source(p.get("source")):
        add("POLICY_UNREVIEWED","出工筛选条件尚未由试点负责人结合专业意见确认；目前可记农活、看天气和比较估时","policy")
    if not p.get("weather_limits"):
        add("WEATHER_LIMITS_UNKNOWN","缺少有来源的天气限制，不能仅凭气温较低就安排出工","policy.weather_limits")
    eligible=[]
    for w in [state["profile"],*state["helpers"]]:
        name=w.get("name",w["id"])
        limits=w.get("limits",{})
        if w.get("state")=="stop":
            add("PERSON_STOP",f"{name}已报告不适，停止为其安排农活；请联系家人并按情况寻求医疗帮助",f"workers.{w['id']}","notice")
            continue
        valid=w.get("state")=="clear" and limits.get("review_status")=="confirmed" and real_source(limits.get("source"))
        tags=[ACTIVITY_ALIASES.get(t,t) for t in limits.get("forbidden_tags",[])]
        if any(t not in ACTIVITY_TAGS for t in tags) or (w.get("limitations_note","").strip() and w.get("limitations_reviewed") is not True):
            valid=False
            add("LIMITATION_MAPPING_UNKNOWN",f"{name}的文字限制还没有落实到禁止动作和具体限制，暂不为其安排",f"workers.{w['id']}","notice")
        try:
            checked=parse_time(w["state_checked_at"])
            valid=valid and checked.astimezone(TZ).date()==now.date() and checked<=now and parse_time(w["limits_valid_until"])>=now
            valid=valid and checked.astimezone(TZ).date().isoformat()==state["settings"]["planning_date"]
        except (ValueError,TypeError,KeyError): valid=False
        if valid and w.get("availability"): eligible.append(w["id"])
        else: add("PERSON_UNCONFIRMED",f"{name}的当天状态、活动限制来源/有效期或可用时间还未填齐",f"workers.{w['id']}","notice")
        load=assess_day_load(w,current_events(state),state["settings"]["planning_date"],now)
        if not load["known"]:
            eligible=[x for x in eligible if x!=w["id"]]
            add("PAST_LOAD_UNKNOWN",f"{name}的当天已活动量还未核对；不知道不能按零处理",f"workers.{w['id']}.used_active_minutes_by_date","notice")
    if not eligible: add("NO_CONFIRMED_WORKER","尚无资料齐全且当前可以参与的人；可先记录，或补充实际帮手","profile")
    for plot in state["plots"]:
        key=plot["id"]
        if not any(t["plot_id"]==key and remaining_for(state,t)["value"]>0 for t in state["tasks"]): continue
        weather=state["weather"].get(key)
        if plot.get("latitude") is None or plot.get("longitude") is None:
            add("LOCATION_UNKNOWN",f"{plot.get('name',key)}还没有位置，无法匹配天气",f"plots.{key}")
        if not weather:
            add("WEATHER_MISSING",f"{plot.get('name',key)}还没有天气资料",f"weather.{key}")
            continue
        if "forecast_run_snapshot" in weather:
            try:
                reference=forecast_reference(weather,now,plot)
                age=(now-reference).total_seconds()/60
                if age<0 or age>p.get("max_forecast_age_minutes",720):
                    add("WEATHER_STALE","这次模型预报已过期，需要更新；重新下载不会改变模型起报时间",f"weather.{key}")
            except (ValueError,TypeError,KeyError):
                add("FORECAST_RUN_INVALID","模型起报、取得时间或原始资料无法对应，请重新取得天气",f"weather.{key}.forecast_run_snapshot")
        elif not weather.get("issued_at"):
            add("ISSUE_TIME_UNKNOWN","预报响应缺少可绑定的签发时刻；不能把下载时间当新预报",f"weather.{key}.issued_at")
        else:
            try:
                age=(now-forecast_reference(weather,now)).total_seconds()/60
                fresh=0<=age<=p.get("max_forecast_age_minutes",720)
            except (ValueError,TypeError,KeyError): fresh=False
            if not fresh:
                add("WEATHER_STALE","天气资料过期或签发时间异常，需要更新",f"weather.{key}")
        feed=weather.get("alert_feed",{})
        for code,message in alert_query_issues(feed,now):
            add(code,message,f"weather.{key}.alert_feed")
        coverage=feed.get("coverage_status") if isinstance(feed,dict) else None
        if not isinstance(coverage,str) or coverage not in {"queried_clear","active_alerts"}:
            add("ALERT_UNKNOWN","当地官方预警尚未完整核对；天气代码不能代替预警",f"weather.{key}.alert_feed")
    templates={t["code"]:t for t in get_community_catalog()["tasks"]}
    for task in state["tasks"]:
        if remaining_for(state,task)["value"]<=0: continue
        label=task.get("name",task["id"])
        template=templates.get(task.get("task_code"))
        if template is None or task.get("operation")!=template["operation"]:
            add("TASK_MAPPING_UNKNOWN",f"{label}的农活种类还没有正确对应到已支持的任务",f"tasks.{task['id']}.task_code")
        elif template.get("engine_schedulable") is not True or task.get("method") not in template["methods"] or task["remaining_quantity"]["unit"] not in template["scheduler_units"]:
            add("METHOD_OR_UNIT_UNSUPPORTED",f"{label}的做法或单位目前只可记录，尚不能用于安排",f"tasks.{task['id']}.method")
        if any(ACTIVITY_ALIASES.get(tag,tag) not in ACTIVITY_TAGS for tag in task.get("tags",[])):
            add("ACTIVITY_MAPPING_UNKNOWN",f"{label}的动作描述还未对应到可核对的动作类型",f"tasks.{task['id']}.tags")
        duty=task.get("wait_duty")
        if duty is not None and (duty.get("status")!="known" or not real_source(duty.get("source"))):
            add("WAIT_UNKNOWN",f"{label}还需确认是否等待、等待多久及依据；等待不会自动算成休息",f"tasks.{task['id']}.wait_duty")
        if remaining_for(state,task)["unknown_records"]:
            add("QUANTITY_UNKNOWN",f"{label}有未确定完成量的反馈，请核对后再安排",f"tasks.{task['id']}")
        if task.get("agronomy",{}).get("status")!="confirmed":
            add("AGRONOMY_UNCONFIRMED",f"{label}的农事条件与期限依据还需确认",f"tasks.{task['id']}.agronomy")
        if task.get("activity_tags_confirmed") is not True or not real_source(task.get("activity_source")):
            add("ACTIVITY_UNKNOWN",f"{label}需要确认实际会有哪些动作，才能核对个人禁做事项",f"tasks.{task['id']}.tags")
        if not any(w in task.get("rates",{}) for w in eligible):
            add("RATE_UNKNOWN",f"{label}缺少可参与者本人的作业估时区间",f"tasks.{task['id']}.rates")
    return {"ready":not any(i["severity"]=="blocking" for i in items),"items":items}


def build_request(state,now=None):
    mode=state["mode"]
    settings=state["settings"]
    start=datetime.fromisoformat(settings["planning_date"]+"T"+settings["start_time"]).replace(tzinfo=TZ)
    end=datetime.fromisoformat(settings["planning_date"]+"T"+settings["end_time"]).replace(tzinfo=TZ)
    stamp=now or (start-timedelta(minutes=15) if mode=="demonstration" else datetime.now(TZ))
    if mode=="real": start=max(start,stamp.replace(second=0,microsecond=0)+timedelta(minutes=1))
    if end<=start: raise CommunityError("这个规划时段已经结束，请选择之后的时间",field="settings.planning_date")
    workers=[]
    for w in [state["profile"],*state["helpers"]]:
        clean=select(w,PERSON_KEYS)
        clean["limits"]={k:v for k,v in clean.get("limits",{}).items() if v is not None}
        clean["limits"]["forbidden_tags"]=[ACTIVITY_ALIASES.get(t,t) for t in clean["limits"].get("forbidden_tags",[])]
        if mode=="real":
            try:
                checked=parse_time(w["state_checked_at"])
                valid=checked<=stamp and checked.astimezone(TZ).date()==stamp.astimezone(TZ).date()==start.date()==end.date() and parse_time(w["limits_valid_until"])>=end
            except (ValueError,TypeError,KeyError): valid=False
            if any(t not in ACTIVITY_TAGS for t in clean["limits"]["forbidden_tags"]) or (w.get("limitations_note","").strip() and w.get("limitations_reviewed") is not True): valid=False
            if not valid: clean["state"]="unknown"
        # 以有时点的自述基数衔接之后实际负荷，已包含在基数里的记录不重复累加。
        used=deepcopy(w.get("used_active_minutes_by_date",{}))
        if mode=="real":
            load=assess_day_load(w,current_events(state),settings["planning_date"],stamp)
            if load["known"]: used[settings["planning_date"]]=load["used_active_minutes"]
            else: clean["state"]="unknown"
        clean["used_active_minutes_by_date"]=used
        workers.append(clean)
    plots=[select(p,{"id","region_id","latitude","longitude","environment","conditions"}) for p in state["plots"]]
    tasks=[]
    for t in state["tasks"]:
        clean=select(t,TASK_KEYS)
        clean["tags"]=[ACTIVITY_ALIASES.get(tag,tag) for tag in clean.get("tags",[])]
        remaining=remaining_for(state,t)
        clean["remaining_quantity"]={"value":remaining["value"],"unit":remaining["unit"]}
        tasks.append(clean)
    resources=[]
    for r in state["resources"]:
        clean=select(r,{"id","kind","capacity","availability","occupation"})
        if r.get("confirmed") is not True: clean["availability"]=[]
        resources.append(clean)
    weather={}
    plot_by_id={p["id"]:p for p in plots}
    for key,value in state["weather"].items():
        if key not in plot_by_id: continue
        if "forecast_run_snapshot" in value:
            try: forecast_reference(value,stamp,plot_by_id[key])
            except (ValueError,TypeError,KeyError):
                raise CommunityError("模型预报原始资料与时点无法对应，请更新天气",field=f"weather.{key}.forecast_run_snapshot") from None
        weather[key]=request_weather(value)
    return {"schema_version":"1.0","mode":"demonstration" if mode=="demonstration" else "assistance","now":stamp.isoformat(),
            "horizon_start":start.isoformat(),"horizon_end":end.isoformat(),"timezone":settings["timezone"],
            "step_minutes":15,"beam_width":12,"max_search_states":1500,"plots":plots,"workers":workers,
            "resources":resources,"tasks":tasks,"weather":weather,"policy":deepcopy(state["policy"])}


class CommunityService:
    def __init__(self,db_path=None,archive_dir=None,*,store=None,alert_fetcher=None):
        if alert_fetcher is not None and not callable(alert_fetcher):
            raise ValueError("预警采集器须为服务器提供的可调用对象")
        self.alert_fetcher=alert_fetcher
        # 账户入口显式注入已绑定身份的存储，不能落回共享的本机数据库。
        if store is not None:
            if db_path is not None or archive_dir is None or not all(callable(getattr(store,k,None)) for k in ("read","change")):
                raise ValueError("注入存储时须明确归档目录，且不能同时指定本机数据库")
            self.store=store
            self.archive_dir=Path(archive_dir)
        else:
            if db_path is None:
                raise ValueError("请明确指定本机数据库或账户存储")
            self.store=CommunityStore(db_path)
            self.archive_dir=Path(archive_dir or Path(db_path).parent/"weather_archive")

    def get_state(self,mode="real"):
        state=self.store.read(mode)
        if mode=="real":
            now=datetime.now(TZ)
            for plan in state["plans"]:
                if plan.get("valid_until") and parse_time(plan["valid_until"])<=now:
                    plan.update(stale=True,stale_reason="安排时段已结束",displayable=False)
                request=plan.get("request",{})
                policy=request.get("policy",state["policy"])
                for plot_id,weather in request.get("weather",state["weather"]).items():
                    try:
                        plot=next((p for p in request.get("plots",state["plots"]) if p["id"]==plot_id),None)
                        age=(now-forecast_reference(weather,now,plot)).total_seconds()/60
                        fresh=0<=age<=policy.get("max_forecast_age_minutes",0)
                    except (ValueError,TypeError,KeyError): fresh=False
                    if not fresh:
                        plan.update(stale=True,stale_reason="安排依据的天气已过期或时间未确认",displayable=False)
                    # 检查当时排程留存的查询，当前天气更新不能替旧计划刷新依据。
                    feed=weather.get("alert_feed",{})
                    issues=alert_query_issues(feed,now)
                    coverage=feed.get("coverage_status") if isinstance(feed,dict) else None
                    if issues:
                        code,message=issues[0]
                        plan.update(stale=True,stale_reason=f"{code}：{message}",displayable=False)
                    elif not isinstance(coverage,str) or coverage not in {"queried_clear","active_alerts"}:
                        plan.update(stale=True,stale_reason="ALERT_UNKNOWN：安排依据的官方预警查询情况未知，请重新核对后安排",displayable=False)
        return state

    def save(self,incoming):
        candidate=validate_state(incoming)
        def change(old):
            if candidate.get("prediction_journal",[])!=old.get("prediction_journal",[]):
                raise CommunityError("事前估时由留存入口追加，不能通过普通编辑改写",field="prediction_journal")
            for key in ("feedback","plans","weather"):
                # GET 的计划有效性可随时间变化，因此只拒绝实质修改，不接受任何客户端派生结果。
                if key!="plans" and candidate[key]!=old[key]:
                    raise CommunityError("完成记录与天气请使用各自入口更新",field=key)
            old_tasks={t["id"]:t for t in old["tasks"]}
            new_tasks={t["id"]:t for t in candidate["tasks"]}
            old_workers={w["id"] for w in [old["profile"],*old["helpers"]]}
            new_workers={w["id"] for w in [candidate["profile"],*candidate["helpers"]]}
            for prediction in old.get("prediction_journal",[]):
                if (prediction.get("task_id") in old_tasks and prediction["task_id"] not in new_tasks) or (prediction.get("worker_id") in old_workers and prediction["worker_id"] not in new_workers):
                    raise CommunityError("已留存估时的农活与人员需保留，才能对应后续实际记录",field="prediction_journal")
            for tid,task in new_tasks.items():
                if task.get("rate_history",[])!=old_tasks.get(tid,{}).get("rate_history",[]):
                    raise CommunityError("估时回顾历史由采用操作生成，不能在普通编辑中改写",field="tasks.rate_history")
            for event in old["feedback"]:
                tid=event["task_id"]
                if tid not in new_tasks:
                    raise CommunityError("已有反馈的农活需要保留，才能核对完成量",field="tasks")
                for key in ("remaining_quantity","plot_id","operation"):
                    if new_tasks[tid].get(key)!=old_tasks[tid].get(key):
                        raise CommunityError("已有反馈的农活不能改计量基数、地块或种类，请另建任务",field="tasks")
            for key in ("settings","profile","plots","tasks","helpers","resources","policy"):
                if key in {"profile","helpers"}:
                    before={w["id"]:w for w in [old["profile"],*old["helpers"]]}
                    rows=[candidate[key]] if key=="profile" else candidate[key]
                    for worker in rows:
                        previous=before.get(worker["id"],{})
                        baseline=deepcopy(previous.get("load_baseline_at_by_date",{}))
                        for day,minutes in worker.get("used_active_minutes_by_date",{}).items():
                            if day not in previous.get("used_active_minutes_by_date",{}) or previous["used_active_minutes_by_date"][day]!=minutes:
                                baseline[day]=now_iso()
                        worker["load_baseline_at_by_date"]={day:at for day,at in baseline.items() if day in worker.get("used_active_minutes_by_date",{})}
                old[key]=candidate[key]
            validate_feedback_archive(old)
            invalidate(old,"资料已更新，请重新安排")
            return old
        return self.store.change(candidate["mode"],candidate["revision"],"edit",change)

    def demo(self):
        old=self.store.read("demonstration")
        return self.store.change("demonstration",old["revision"],"load_demo",lambda _:make_demo())

    def refresh_weather(self,data):
        if not isinstance(data,dict) or set(data)-{"mode","revision","plot_id"}:
            raise CommunityError("天气更新只接受模式、资料版本和已保存的地块编号")
        if type(data.get("revision")) is not int or data["revision"]<0:
            raise CommunityError("请提供有效资料版本",field="revision")
        state=self.store.read(data.get("mode","real"))
        if state["mode"]!="real": raise CommunityError("演示天气保持虚构；请回到真实记录查看预报")
        if data.get("revision")!=state["revision"]: raise CommunityError("资料已更新，请重新读取",code="REVISION_CONFLICT",status=409)
        plot=next((p for p in state["plots"] if p["id"]==data.get("plot_id")),None)
        if plot is None: raise CommunityError("没有找到这个地块")
        weather=deepcopy(fetch_weather(plot,self.archive_dir))
        alerts=self._refresh_alerts(plot)
        feed=alerts["feed"]
        known=feed["coverage_status"] in {"queried_clear","active_alerts"}
        weather["alert_feed"]=feed
        weather["alert_status"]=feed["coverage_status"]
        weather["alert_acquisition"]={"code":alerts["code"],"message":alerts["message"]}
        tags=sorted({item["hazard_tag"] for item in feed["items"]})
        identifiers=[item["id"] for item in feed["items"]]
        for record in weather["records"]:
            if known:
                record["hazards"]=list(tags)
                record["hazard_item_ids"]=list(identifiers)
            else:
                record.pop("hazards",None)
                record.pop("hazard_item_ids",None)
        provenance=weather.setdefault("provenance",{})
        provenance.pop("official_alert_acquisition",None)
        if known:
            # 这里只证明服务器回调结果与原响应一致，不宣称独立认证提供方身份。
            provenance["official_alert_acquisition"]="server_callback_snapshot_checked"
            weather["warnings"]=[
                "这是户外格点天气预报；已取得该地块的官方预警查询快照，查询结果不代表个人健康安全。"
                if message in {"这是户外格点天气预报，未核对官方预警。","这是户外格点天气预报，仍需另行核对官方预警。"} else message
                for message in weather.get("warnings",[])]
        if alerts["message"] not in weather.setdefault("warnings",[]):
            weather["warnings"].append(alerts["message"])
        def change(old):
            old["weather"][plot["id"]]=weather
            invalidate(old,"天气已更新，请重新安排")
            return old
        return {"state":self.store.change("real",data["revision"],"weather",change),"weather":weather}

    def _refresh_alerts(self,plot):
        """只接受完整原生成功快照或明确未知；不把回调异常内容保存为用户资料。"""
        from .official_alert_fetch import ALERT_RESULT_MESSAGES
        from .qweather_alert_snapshot import validate_qweather_snapshot

        def unknown(code):
            return {"feed":{"source":"QWeather official weather alerts","protocol":"official_equivalent",
                "issued_at":None,"queried_at":None,"http_status":None,"coverage_status":"not_queried",
                "query":{"type":"point","latitude":plot["latitude"],"longitude":plot["longitude"],
                         "snapshot_complete":False},"items":[]},
                "code":code,"message":ALERT_RESULT_MESSAGES[code]}

        if self.alert_fetcher is None:
            return unknown("ALERT_NOT_CONFIGURED")
        try:
            result=self.alert_fetcher(deepcopy(plot),self.archive_dir)
        except Exception:
            return unknown("ALERT_CALLBACK_FAILED")
        try:
            if not isinstance(result,dict) or set(result)!={"feed","code","message"}:
                raise ValueError("回调结构无效")
            code=result["code"]
            if not isinstance(code,str) or code not in ALERT_RESULT_MESSAGES:
                raise ValueError("回调状态无效")
            feed=deepcopy(result["feed"])
            if not isinstance(feed,dict):
                raise ValueError("回调信封无效")
            now=datetime.now(TZ)
            query=feed.get("query")
            if (not isinstance(query,dict) or query.get("type")!="point"
                    or any(type(query.get(key)) not in (int,float) or query[key]!=plot[key]
                           for key in ("latitude","longitude"))):
                raise ValueError("回调地点不匹配")
            if alert_query_issues(feed,now):
                raise ValueError("查询快照或时效未通过")
            if code=="ALERT_QUERY_OK":
                validate_qweather_snapshot(feed,now)
                if feed.get("coverage_status") not in {"queried_clear","active_alerts"}:
                    raise ValueError("成功查询状态无效")
            else:
                # 失败信封闭合校验，避免保存任意回调字段或假造一次实际查询。
                allowed={"source","protocol","issued_at","queried_at","http_status","coverage_status","query","items"}
                coverage=feed.get("coverage_status")
                if (set(feed)!=allowed or feed.get("source")!="QWeather official weather alerts"
                        or feed.get("protocol")!="official_equivalent" or feed.get("issued_at") is not None
                        or feed.get("items")!=[] or not isinstance(coverage,str)
                        or coverage not in {"not_queried","feed_unavailable","query_incomplete"}
                        or set(query)!={"type","latitude","longitude","snapshot_complete"}
                        or query.get("snapshot_complete") is not False):
                    raise ValueError("未知查询状态无效")
                status=feed.get("http_status")
                if status is not None and (type(status) is not int or not 100<=status<=599):
                    raise ValueError("未知查询HTTP状态无效")
                if coverage=="not_queried":
                    if feed.get("queried_at") is not None or status is not None:
                        raise ValueError("未查询不能填写实际查询证据")
                elif parse_time(feed.get("queried_at"))>now:
                    raise ValueError("查询时刻在未来")
            # 文案由程序固定码表提供，忽略回调传入的任意message，避免带出密钥或异常。
            return {"feed":feed,"code":code,"message":ALERT_RESULT_MESSAGES[code]}
        except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
            return unknown("ALERT_CALLBACK_INVALID")

    def import_weather(self,data):
        if data.get("mode")!="real":
            raise CommunityError("真实天气快照只能导入真实记录区")
        state=self.store.read("real")
        plot=next((p for p in state["plots"] if p["id"]==data.get("plot_id")),None)
        snapshot=data.get("snapshot")
        if plot is None or not isinstance(snapshot,dict) or snapshot.get("format")!="yilao-weather-snapshot-v1":
            raise CommunityError("请选择地块和支持的天气快照文件")
        weather=snapshot.get("weather")
        provenance=snapshot.get("provenance")
        if not isinstance(weather,dict) or weather.get("kind")!="forecast" or not isinstance(provenance,dict):
            raise CommunityError("快照须包含预报天气及其来源记录")
        if not real_source(weather.get("source")):
            raise CommunityError("预报来源不能留空或写待确认",field="weather.source")
        allowed_weather=WEATHER_REQUEST_KEYS|{"retrieved_at","retrieved_time_basis","imported_at","issue_time_status","location","alert_status","provenance","warnings","alert_acquisition"}
        if set(weather)-allowed_weather:
            raise CommunityError("天气快照包含不支持的字段",field="weather")
        for key in ("source_url","source_checked_by","checked_at","issue_time_basis"):
            if not real_source(provenance.get(key)): raise CommunityError("快照缺少来源核对信息",field="provenance."+key)
        try:
            source_url=urlparse(provenance["source_url"])
            valid_url=source_url.scheme=="https" and bool(source_url.hostname) and not source_url.username and not source_url.password and not any(c.isspace() for c in source_url.netloc)
        except ValueError:
            valid_url=False
        if not valid_url:
            raise CommunityError("快照需注明HTTPS来源网址",field="provenance.source_url")
        now=datetime.now(TZ)
        try:
            checked_at=parse_time(provenance["checked_at"])
        except (ValueError,TypeError):
            raise CommunityError("来源核对时间格式无效",field="provenance.checked_at") from None
        if checked_at>now:
            raise CommunityError("来源核对时间不能在未来")
        mini={"schema_version":"1.0","mode":"assistance","now":now.isoformat(),"horizon_start":now.isoformat(),
              "horizon_end":(now+timedelta(hours=1)).isoformat(),"timezone":"Asia/Shanghai",
              "plots":[select(plot,{"id","region_id","latitude","longitude","environment","conditions"})],
              "workers":[],"tasks":[],"resources":[],"weather":{plot["id"]:request_weather(weather)},"policy":empty_state()["policy"]}
        is_run="forecast_run_snapshot" in weather
        if is_run:
            try: forecast_reference(weather,now,plot)
            except (ValueError,TypeError,KeyError):
                raise CommunityError("模型预报原始资料与时点无法对应",field="weather.forecast_run_snapshot") from None
        checked=request_weather(validate_request(mini)["weather"][plot["id"]])
        if is_run:
            received=weather["forecast_run_snapshot"]["retrieved_at"]
            received_basis="forecast_run_snapshot"
        elif weather.get("retrieved_at"):
            try:
                received_time=parse_time(weather["retrieved_at"])
                if received_time>now: raise ValueError("future")
            except (ValueError,TypeError):
                raise CommunityError("原资料取得时间无效，不能在未来",field="weather.retrieved_at") from None
            received=weather["retrieved_at"]
            received_basis="input_declaration_original_retrieval"
        else:
            received=now.isoformat()
            received_basis="import_received_original_unknown"
        checked.update(retrieved_at=received,imported_at=now.isoformat(),retrieved_time_basis=received_basis,
                       issue_time_status="model_run_reference_no_issue_time" if is_run else "user_supplied_source_binding",
                       location={"latitude":plot["latitude"],"longitude":plot["longitude"]},
                       alert_status=checked.get("alert_feed",{}).get("coverage_status","not_queried"),provenance={**provenance,"verification":"input_declaration_not_independent_authentication"},
                       warnings=["这份快照的来源核对由导入者声明；软件检查字段和时间，不认证外部来源。"])
        if received_basis=="import_received_original_unknown":
            checked["warnings"].append("记录的是此次导入收到时间；原资料何时取得尚未提供。")
        def change(old):
            old["weather"][plot["id"]]=checked
            invalidate(old,"导入新的天气快照，请重新安排")
            return old
        return {"state":self.store.change("real",data.get("revision"),"weather_import",change),"weather":checked}

    def plan(self,data):
        state=self.get_state(data.get("mode","real"))
        if data.get("revision")!=state["revision"]: raise CommunityError("资料已更新，请重新读取",code="REVISION_CONFLICT",status=409)
        ready=readiness(state)
        if not ready["ready"]: return {"state":state,"plan":None,"readiness":ready,"estimates":estimates(state)}
        request=build_request(state)
        result=run_checked(request,max_wall_seconds=20)
        independent=check_plan(request,result)
        independent_valid=independent.get("valid",independent.get("ok",False))
        if not independent_valid:
            raise CommunityError("独立复算未通过，已保留资料但未发布安排",code="PLAN_CHECK_FAILED",status=422)
        names={t["id"]:t.get("name",t["id"]) for t in state["tasks"]}
        people={w["id"]:w.get("name",w["id"]) for w in [state["profile"],*state["helpers"]]}
        sessions=[dict(s,task_name=names[s["task_id"]],worker_name=people[s["worker_id"]],segments=s.get("phases",[])) for s in result["sessions"]]
        reason_labels={"DAILY_ACTIVE_LIMIT":"这一天的活动量已经达到填入的个人限制。",
            "DEADLINE_OR_HORIZON":"加上往返与休息后，剩余时间不够安排这段农活。",
            "TASK_PARALLELISM":"尚未确认这项农活能由多人同时分工。",
            "WORKER_CONFLICT_OR_REST":"参与者在该时段另有安排，或两段之间休息不足。",
            "RESOURCE_UNAVAILABLE":"需要的工具、饮水或休息处在该时段不能使用。",
            "WORKER_STATE_NOT_CLEAR":"参与者的当天身体情况还没确认，或已报告不适。"}
        task_results=[]
        for t in result["task_results"]:
            display=[]
            for reason in t.get("reasons",[]):
                code=reason.get("code","")
                label=reason_labels.get(code)
                if label is None:
                    if any(word in code for word in ("TEMPERATURE","WIND","RAIN","PRECIPITATION","HAZARD","WEATHER","RADIATION","DAYLIGHT")):
                        label="这个时段的天气或日照条件没有通过已填写的出工要求。"
                    else: label="这段安排还有条件未满足，可在计算详情中核对。"
                if label not in display: display.append(label)
            task_results.append(dict(t,name=names[t["task_id"]],display_reasons=display))
        alternatives=[]
        for task in task_results:
            if task["remaining_quantity"]>0:
                alternatives.append({"task_id":task["task_id"],"remaining_quantity":task["remaining_quantity"],"unit":task["unit"],"actions":["核实帮手可用时间与其本人的估时","比较实际可用工具对应的工作量","先确认能否分批，以及农技上允许的改期条件"],"confirmed":False})
        plan={"id":uuid.uuid4().hex,"created_at":now_iso(),"mode":state["mode"],"state_revision":state["revision"],
              "algorithm_version":result["algorithm_version"],
              "status":result["status"],"displayable":True,"label":"虚构演示安排" if state["mode"]=="demonstration" else "安排草稿",
              "sessions":sessions,"task_results":task_results,"warnings":result["warnings"],"alternatives":alternatives,
              "verification":{"engine":result["verification"],"independent":independent},"input_sha256":sha256(json_text(request).encode()).hexdigest(),
              "stale":False,"valid_until":request["horizon_end"],"search":result["search"],"request":request,
              "display_warnings":["按已填资料生成，出工前仍要确认身体、天气和现场条件。","剩余部分可以继续核实帮手、工具或分批安排；不通过减少必要休息来赶工。"],
              "meaning":"安排不等于实际完成；输入条件下可安排不等于已证明健康安全。"}
        def change(old):
            invalidate(old,"已生成新版本")
            old["plans"].append(plan)
            old["plans"]=old["plans"][-200:]
            return old
        saved=self.store.change(state["mode"],state["revision"],"plan",change)
        return {"state":saved,"plan":plan,"readiness":ready}

    def feedback(self,data):
        mode=data.get("mode","real")
        state=self.store.read(mode)
        allowed={"event_id","task_id","worker_id","status","completed_quantity","started_at","ended_at","net_minutes","rest_minutes","quantity_source","clock_source","notes","consent","plan_id","supersedes_event_id","context_matches_task","refresh_context","prediction_id"}
        event={k:deepcopy(v) for k,v in data.items() if k in allowed}
        identity=sha256(json_text(event).encode()).hexdigest()
        for previous in state["feedback"]:
            if previous["event_id"]==event.get("event_id"):
                if previous.get("submission_sha256")!=identity:
                    raise CommunityError("同一记录编号对应不同内容，已停止重复提交",code="EVENT_CONFLICT",status=409)
                task=next(t for t in state["tasks"] if t["id"]==previous["task_id"])
                return {"state":state,"remaining":remaining_for(state,task),"idempotent":True}
        if not isinstance(event.get("event_id"),str) or not 8<=len(event["event_id"])<=100:
            raise CommunityError("完成记录需要唯一编号",field="event_id")
        task=next((t for t in state["tasks"] if t["id"]==event.get("task_id")),None)
        if task is None: raise CommunityError("农活不存在",field="task_id")
        if event.get("worker_id") not in {w["id"] for w in [state["profile"],*state["helpers"]]}:
            raise CommunityError("请选择实际参与的人",field="worker_id")
        supersedes=event.get("supersedes_event_id")
        original=None
        if supersedes:
            original=next((e for e in current_events(state) if e["event_id"]==supersedes),None)
            if original is None or original["task_id"]!=event["task_id"] or original["worker_id"]!=event["worker_id"]:
                raise CommunityError("只能更正同一农活、同一人的当前记录",field="supersedes_event_id")
            if "prediction_id" in event and event["prediction_id"]!=original.get("prediction_id"):
                raise CommunityError("更正时保留原估时关联，不能在知道结果后更换预测",field="prediction_id")
            if original.get("prediction_id") is not None:
                event["prediction_id"]=original["prediction_id"]
        if event.get("prediction_id") is not None:
            prediction=next((p for p in state.get("prediction_journal",[]) if p.get("prediction_id")==event["prediction_id"]),None)
            if prediction is None or prediction.get("task_id")!=event["task_id"] or prediction.get("worker_id")!=event["worker_id"]:
                raise CommunityError("请选择同一农活、同一人的已留存估时",field="prediction_id")
        if event.get("consent") is not True:
            raise CommunityError("请确认本人同意在此设备保存这条作业记录",field="consent")
        status=event.get("status")
        if status not in {"completed","partial","not_done","interrupted","unknown"}:
            raise CommunityError("请选择实际完成情况",field="status")
        value,unit=canonical_quantity(event.get("completed_quantity"))
        if status=="unknown":
            if value is not None: raise CommunityError("未确定的完成情况不能填写完成量",field="completed_quantity")
        else:
            if value is None: raise CommunityError("请填实际完成量；未做请明确填0",field="completed_quantity")
            if status=="not_done" and value!=0: raise CommunityError("未做的完成量须为0",field="completed_quantity")
            if status in {"completed","partial"} and value<=0: raise CommunityError("已做或部分完成需要正完成量",field="completed_quantity")
            _,task_unit=canonical_quantity(task["remaining_quantity"])
            if unit!=task_unit: raise CommunityError("完成量单位与任务不一致",field="completed_quantity.unit")
            for key in ("quantity_source","clock_source"):
                if not real_source(event.get(key)):
                    raise CommunityError("请记实际观察或回忆来源，不能照抄计划",field=key)
            if not is_untimed_not_done(event):
                try: a,b=parse_time(event["started_at"]),parse_time(event["ended_at"])
                except (KeyError,ValueError,TypeError): raise CommunityError("请填实际开始、结束时间",field="started_at") from None
                if b<a or b>datetime.now(TZ)+timedelta(minutes=5):
                    raise CommunityError("实际作业时间不能倒置或在未来",field="ended_at")
                net=finite(event.get("net_minutes"),"net_minutes",0,1440)
                rest=finite(event.get("rest_minutes",0),"rest_minutes",0,1440)
                if net+rest>(b-a).total_seconds()/60+1e-6:
                    raise CommunityError("净劳动与休息分钟超过实际经过时间",field="net_minutes")
                if value>0 and net<=0: raise CommunityError("有完成量时需要记录正净劳动时间",field="net_minutes")
        if event.get("plan_id") and event["plan_id"] not in {p["id"] for p in state["plans"]}:
            raise CommunityError("关联安排不存在",field="plan_id")
        event.update(submission_sha256=identity,recorded_at=now_iso(),evidence_type="SELF_REPORT" if mode=="real" else "SYNTHETIC",verification_status="unverified")
        if original is not None and ("imported_at" in original or original.get("evidence_type")=="IMPORTED_SELF_REPORT"):
            # 更正保留导入来源，不能把外来记录洗成本机真实观察。
            event.update(imported_at=original.get("imported_at"),evidence_type="IMPORTED_SELF_REPORT")
        refresh_context=event.pop("refresh_context",False)
        if type(refresh_context) is not bool or (event.get("context_matches_task") is not None and type(event["context_matches_task"]) is not bool):
            raise CommunityError("请明确实际情境是否一致；未确认可留空",field="context_matches_task")
        if refresh_context and type(event.get("context_matches_task")) is not bool:
            raise CommunityError("重新核对情境时，请明确选择相同或不同",field="context_matches_task")
        if original is not None and not refresh_context:
            # 更正数量不会把原先的作业情境替换成今日已改过的任务资料。
            event["context_snapshot"]=deepcopy(original.get("context_snapshot"))
            event["context_matches_task"]=original.get("context_matches_task")
        else:
            from .community_learning import capture_rate_context
            event["context_snapshot"]=capture_rate_context(state,event["task_id"],event["worker_id"],captured_at=event["recorded_at"])
            event.setdefault("context_matches_task",None)
        def change(old):
            old["feedback"].append(event)
            validate_feedback_archive(old)
            invalidate(old,"收到实际作业记录，请按新的剩余量安排")
            return old
        saved=self.store.change(mode,data.get("revision"),"feedback",change)
        return {"state":saved,"remaining":remaining_for(saved,task),"idempotent":False}

    def prediction_preview(self,data):
        from .community_prediction import preview_prediction
        return preview_prediction(self.get_state(data.get("mode","real")),data.get("task_id"),data.get("worker_id"),data.get("quantity"))

    def freeze_prediction(self,data):
        from .community_prediction import freeze_prediction
        def change(old):
            prediction=freeze_prediction(old,data,now=datetime.now(TZ))
            old.setdefault("prediction_journal",[]).append(prediction)
            invalidate(old,"已留存新的事前估时，请重新核对安排")
            return old
        return self.store.change(data.get("mode","real"),data.get("revision"),"freeze_prediction",change)

    def prediction_review(self,mode="real"):
        from .community_prediction import review_predictions
        return review_predictions(self.get_state(mode),now=datetime.now(TZ))

    def rate_review(self,data):
        from .community_learning import review_personal_rates
        state=self.get_state(data.get("mode","real"))
        task_id,worker_id=data.get("task_id"),data.get("worker_id")
        if not isinstance(task_id,str) or not isinstance(worker_id,str):
            raise CommunityError("请选择要回顾的农活和人员")
        if task_id not in {t["id"] for t in state["tasks"]} or worker_id not in {w["id"] for w in [state["profile"],*state["helpers"]]}:
            raise CommunityError("要回顾的农活或人员不存在")
        return {"review":review_personal_rates(state,task_id,worker_id)}

    def adopt_rate_review(self,data):
        from .community_learning import review_personal_rates
        if data.get("confirmed") is not True:
            raise CommunityError("需要本人确认采用这份自述估时；它尚未实测校准",field="confirmed")
        task_id,worker_id=data.get("task_id"),data.get("worker_id")
        if not isinstance(task_id,str) or not isinstance(worker_id,str):
            raise CommunityError("请选择农活和人员")
        adopted={}
        def change(old):
            task=next((t for t in old["tasks"] if t["id"]==task_id),None)
            if task is None or worker_id not in {w["id"] for w in [old["profile"],*old["helpers"]]}:
                raise CommunityError("农活或人员不存在")
            review=review_personal_rates(old,task_id,worker_id)
            if data.get("basis_sha256")!=review["basis_sha256"]:
                raise CommunityError("依据已经变化，请重新查看估时回顾后再确认",code="REVIEW_CHANGED",status=409)
            proposal=review.get("conservative_proposal")
            if not proposal or review.get("adoption",{}).get("blocked") is not False:
                raise CommunityError("这份记录还不足以采用，请先核对回顾中列出的情况",code="RATE_REVIEW_BLOCKED")
            rate=select(proposal,{"unit","scope","low","high"})
            finite(rate.get("low"),"rate.low",1e-8,1e7)
            finite(rate.get("high"),"rate.high",rate["low"],1e7)
            rate["source"]="本人复核的历史自述估时（非实测校准）；依据 "+review["basis_sha256"][:16]
            prior=deepcopy(task.get("rates",{}).get(worker_id))
            # 只采用明确候选，不触碰个人活动限额；原估时和依据留在任务历史中。
            task.setdefault("rates",{})[worker_id]=rate
            task.setdefault("rate_history",[]).append({"at":now_iso(),"worker_id":worker_id,"previous_rate":prior,
                "adopted_rate":deepcopy(rate),"basis_sha256":review["basis_sha256"],"record_ids":review["included_record_ids"],
                "source_class":"self_report_review","field_verified":False,"calibrated":False,"confirmed_by_user":True})
            invalidate(old,"本人采用了新的估时依据，请重新安排")
            adopted.update(review=review,adopted_rate=rate)
            return old
        saved=self.store.change(data.get("mode","real"),data.get("revision"),"adopt_rate_review",change)
        return {"state":saved,**adopted}

    def field_collection(self,mode,*,export_secret):
        from .field_collection_export import FieldCollectionExportError, export_field_collection
        try:
            return export_field_collection(self.get_state(mode),export_secret=export_secret)
        except FieldCollectionExportError:
            raise CommunityError("记录格式暂不能整理为待复核材料，请先核对原记录；保存内容未改变",
                                 code="COLLECTION_EXPORT_INVALID",status=422) from None

    def export(self,mode):
        state=self.get_state(mode)
        return {"format":"yilao-community-export-v1","exported_at":now_iso(),"state":state,"checksum":sha256(json_text(state).encode()).hexdigest()}

    def import_bundle(self,data):
        bundle=data.get("bundle",{})
        if not isinstance(bundle,dict) or bundle.get("format")!="yilao-community-export-v1":
            raise CommunityError("导入文件格式不正确")
        candidate=validate_state(bundle.get("state"))
        if candidate["mode"]!=data.get("mode"):
            raise CommunityError("演示与真实资料不能相互导入",code="MODE_CONFLICT")
        if bundle.get("checksum")!=sha256(json_text(bundle["state"]).encode()).hexdigest():
            raise CommunityError("导入文件校验不匹配，原资料未改动",code="CHECKSUM_MISMATCH")
        validate_feedback_archive(candidate)
        original_candidate=deepcopy(candidate)
        # 文件校验只验证传输完整性，不认证文件所声称的来源或现场效果。
        for prediction in candidate.get("prediction_journal",[]):
            prediction.update(trust_status="imported_unverified",imported_at=now_iso())
        for event in candidate["feedback"]:
            event["verification_status"]="unverified"
            event["imported_at"]=now_iso()
            if candidate["mode"]=="real": event["evidence_type"]="IMPORTED_SELF_REPORT"
        for task in candidate["tasks"]:
            if "rate_history" not in task: continue
            history=[]
            for item in task["rate_history"]:
                # 导出校验码不认证外来文件中的校准、实测或采用声明。
                clean=select(item,{"at","worker_id","previous_rate","adopted_rate","basis_sha256","record_ids"})
                clean.update(source_class="imported_unverified_self_report",field_verified=False,calibrated=False,
                             confirmed_by_user=False,n_real=0,claim_status="unverified_import",imported_at=now_iso())
                history.append(clean)
            task["rate_history"]=history
        invalidate(candidate,"导入的历史安排需重新核对")
        candidate["weather"]={}
        for task in candidate["tasks"]: remaining_for(candidate,task)
        def change(old):
            # 已在本机留存的预测与结果不可通过恢复旧备份悄悄抹掉。
            prior_journal=old.get("prediction_journal",[])
            merged=deepcopy(prior_journal)
            prior_ids={p["prediction_id"]:p for p in prior_journal}
            transport={"trust_status","imported_at"}
            for row in candidate.get("prediction_journal",[]):
                previous=prior_ids.get(row["prediction_id"])
                if previous is None:
                    merged.append(row)
                elif select(previous,set(previous)-transport)!=select(row,set(row)-transport):
                    raise CommunityError("导入估时与本机同编号内容冲突，原留存未改动",code="PREDICTION_CONFLICT",field="prediction_journal",status=409)
            candidate["prediction_journal"]=merged
            if prior_journal:
                incoming_by_id={e["event_id"]:e for e in original_candidate["feedback"]}
                imported_by_id={e["event_id"]:e for e in candidate["feedback"]}
                feedback=deepcopy(old["feedback"])
                existing={e["event_id"] for e in feedback}
                event_transport={"imported_at","evidence_type","verification_status"}
                for previous in feedback:
                    row=incoming_by_id.get(previous["event_id"])
                    if row is not None and select(previous,set(previous)-event_transport)!=select(row,set(row)-event_transport):
                        raise CommunityError("导入结果与已留存的实际记录冲突，请使用更正入口",code="EVENT_CONFLICT",field="feedback",status=409)
                feedback.extend(row for ident,row in imported_by_id.items() if ident not in existing)
                candidate["feedback"]=feedback
                validate_feedback_archive(candidate)
            return candidate
        return self.store.change(data["mode"],data.get("revision"),"import",change)
