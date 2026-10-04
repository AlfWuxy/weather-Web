"""文件入口：不发送通知，不连接或修改正式天气通服务。

输出采用“先写同目录临时文件，再原子替换”，且只在计算成功后替换；
计算被取消/超时或报错时不写任何输出，因此磁盘上不会留下半写入的计划。
"""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile

from .audit import verify_plan
from .engine import PlanInterrupted, plan
from .migrate import migrate_request
from .models import InputError, validate_request
from .recalc import check_plan
from .workload import review_observations


def _load(filename):
    path = Path(filename)
    if path.stat().st_size > 20_000_000:
        raise InputError("输入文件超过20MB，请按规划窗口分批")
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_all(payloads):
    """先全部写入同目录临时文件，再逐个原子替换（rename）。

    目标文件要么保持旧版本，要么是新版本，绝不会被截断或写入一半；
    真正写盘发生在临时文件上，所以即使写临时文件阶段失败，目标文件也不受影响。
    本实现不使用任何删除接口（不 unlink/remove），故障时可能残留 .tmp- 临时文件，
    但目标是原始输出文件保持一致。
    """
    staged = []
    for path, text in payloads:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp = tempfile.mkstemp(prefix=".tmp-" + path.name + "-", dir=str(path.parent))
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        staged.append((Path(tmp), path))
    for tmp, path in staged:
        os.replace(tmp, path)
    return [path for _, path in staged]


def merge_patch(base, patch):
    """对象递归覆盖，数组整体替换；原输入保持不变。"""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return deepcopy(patch)
    result = deepcopy(base)
    for key, value in patch.items():
        result[key] = merge_patch(result.get(key), value)
    return result


def run_checked(request, *, cancel=None, max_wall_seconds=None):
    result = plan(request, cancel=cancel, max_wall_seconds=max_wall_seconds)
    result["verification"] = verify_plan(request, result)
    if not result["verification"]["valid"]:
        raise RuntimeError("排程复查失败: " + "; ".join(result["verification"]["errors"]))
    return result


def render_summary(result):
    labels = {"complete": "输入约束下已安排全部任务", "partial": "部分任务尚未安排", "no_plan_found": "本次搜索未找到安排"}
    lines = ["# 宜老农业算法运行结果", "",
             f"状态：{labels[result['status']]}。模式：{result['mode']}。",
             f"算法版本：{result['algorithm_version']}；输入标识：{result['plan_id']}。", "",
             "## 任务与未安排量", "",
             "| 任务 | 本次待做 | 已安排 | 未安排 | 单位 |",
             "|---|---:|---:|---:|---|"]
    for task in result["task_results"]:
        lines.append(f"| {task['task_id']} | {task['requested_quantity']:.4g} | {task['scheduled_quantity']:.4g} | {task['remaining_quantity']:.4g} | {task['unit']} |")
    lines += ["", "## 具体出工", "", "| 人员 | 任务 | 开始 | 结束 | 工作量 | 活动/休息分钟 |", "|---|---|---|---|---|---|"]
    for s in result["sessions"]:
        lines.append(f"| {s['worker_id']} | {s['task_id']} | {s['start']} | {s['end']} | {s['quantity']:.4g} {s['unit']} | {s['active_minutes']:g} / {s['rest_minutes']:g} |")
    lines += ["", "## 解释与适用范围", ""]
    lines += ["- " + w for w in result["warnings"]]
    for task in result["task_results"]:
        if task["remaining_quantity"] > 1e-8:
            lines += ["", f"**{task['task_id']} 的候选排除记录**（只代表尝试过的候选）：", ""]
            lines += ["- " + item["code"] + "：" + item["message"] for item in task["reasons"]]
    search = result["search"]
    lines += ["", "## 计算与复查", "",
              f"- 检查候选：{search['candidate_checks']}；扩展状态：{search['states_explored']}。",
              f"- 状态预算用尽：{search['budget_exhausted']}；候选检查预算用尽：{search['candidate_check_limit_reached']}。"]
    fair = search.get("fair_allocation") or {}
    if fair:
        lines.append(f"- 候选检查预算按(任务,人)对等分：每对上限 {fair.get('fair_share_cap_per_pair')}；"
                     f"未充分探索组合 {len(fair.get('unexplored_pairs') or [])} 个。")
        lines.append("- 预算用尽只表示本次有界搜索未穷尽，不表示任何安排都做不成。")
    lines += ["- 未证明全局最优，也未证明剩余工作在任何安排下都无法完成。",
              f"- 独立时间轴复查：{result.get('verification', {}).get('valid', '未执行')}。", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="宜老农业：有来源约束下的农事排程")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="计算并复查排程")
    p.add_argument("input")
    p.add_argument("--output")
    p.add_argument("--summary")
    p.add_argument("--max-wall-seconds", type=float, default=None,
                   help="墙钟上限；超时则中断且不写任何输出")
    p.add_argument("--cancel-file", default=None,
                   help="外部取消：该文件出现即中断（用于超时/中断可恢复性检查）")
    v = sub.add_parser("validate", help="只检查输入结构，不认证来源或安全")
    v.add_argument("input")
    s = sub.add_parser("scenarios", help="重算天气/人手/效率变化情境")
    s.add_argument("input")
    s.add_argument("patches")
    s.add_argument("--output")
    r = sub.add_parser("review", help="同口径真实记录的描述性复盘（可选口径时间范围）")
    r.add_argument("input")
    r.add_argument("observations")
    r.add_argument("--task", required=True)
    r.add_argument("--worker", required=True)
    r.add_argument("--output")
    mem = sub.add_parser("members", help="多预报成员/区间天气情境下的排程稳健检查（计数，不是概率）")
    mem.add_argument("input")
    mem.add_argument("memberset")
    mem.add_argument("--plan", help="已承诺计划 JSON；省略则对当前输入现算一份")
    mem.add_argument("--mode", choices=["committed", "replan"], default="committed")
    mem.add_argument("--output")
    m = sub.add_parser("migrate", help="显式迁移旧请求到 schema 1.0；不升级真实性或审核等级")
    m.add_argument("input")
    m.add_argument("--output")
    m.add_argument("--drop-unknown-fields", action="store_true",
                   help="显式丢弃未知字段；默认拒绝。不会抬高真实性或审核等级")
    rc = sub.add_parser("recalc", help="独立复算数量与U05时间分段；不调用排程内部函数")
    rc.add_argument("input")
    rc.add_argument("plan")
    rc.add_argument("--output")
    cv = sub.add_parser("coverage", help="作物组×环节覆盖状态表；未支持项逐条可见（只读）")
    cv.add_argument("--request", help="可选：核对某请求的作物/环节是否在覆盖表内")
    cv.add_argument("--output")
    cv.add_argument("--summary")
    ar = sub.add_parser("advice-replan",
                        help="天气变化重排：新预报产出新建议版本，旧版保留（不悄悄改写已展示建议）")
    ar.add_argument("input")
    ar.add_argument("newweather", help="新预报 JSON：{plot_id: weather}；来源不得为占位词")
    ar.add_argument("--plot", default=None, help="地块；省略则取 newweather 中的第一个")
    ar.add_argument("--ledger", default=None, help="既有建议台账 JSON（可选）")
    ar.add_argument("--save-ledger", default=None, help="写出更新后的建议台账 JSON（可选）")
    ar.add_argument("--previous-was-displayed", action="store_true",
                    help="把台账中的当前建议视为已展示，用于核对是否必须重新展示")
    ar.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        inputs = [Path(getattr(args, key)).resolve()
                  for key in ("input", "patches", "observations", "plan", "request", "memberset",
                              "newweather", "ledger")
                  if getattr(args, key, None) is not None]
        outputs = [Path(getattr(args, key)).resolve()
                   for key in ("output", "summary", "save_ledger") if getattr(args, key, None)]
        if len(set(outputs)) != len(outputs) or set(inputs) & set(outputs):
            raise InputError("输出路径不能覆盖输入，也不能互相重名")
        raw = None if args.command == "coverage" else _load(args.input)
        save_ledger_text = None
        if args.command == "coverage":
            from .coverage_status import build_matrix, request_coverage

            matrix = build_matrix()
            result = matrix
            if getattr(args, "request", None):
                result = dict(matrix)
                result["request_coverage"] = request_coverage(matrix, _load(args.request))
        elif args.command == "validate":
            validated = validate_request(raw)
            result = {"structurally_valid": True, "mode": validated["mode"], "meaning": "仅输入结构有效，不代表可排程、来源已核实或健康安全"}
        elif args.command == "migrate":
            result = migrate_request(raw, drop_unknown_fields=args.drop_unknown_fields)
        elif args.command == "plan":
            cancel = None
            cancel_file = getattr(args, "cancel_file", None)
            if cancel_file:
                cancel_path = str(cancel_file)
                cancel = (lambda p: (lambda: Path(p).exists()))(cancel_path)
            result = run_checked(raw, cancel=cancel,
                                 max_wall_seconds=getattr(args, "max_wall_seconds", None))
        elif args.command == "scenarios":
            patches = _load(args.patches)
            if not isinstance(patches, list):
                raise InputError("scenarios: 必须为数组")
            seen, runs = set(), []
            for item in patches:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("patch"), dict):
                    raise InputError("scenario: 需要id和patch对象")
                if item["id"] in seen:
                    raise InputError("scenario: id不能重复")
                seen.add(item["id"])
                runs.append({"id": item["id"], "description": item.get("description", ""),
                             "result": run_checked(merge_patch(raw, item["patch"]))})
            result = {"baseline": run_checked(raw), "scenarios": runs,
                      "meaning": "按明确输入情境重算，不代表天气概率或健康效果预测"}
        elif args.command == "members":
            from .weather_members import check_committed_plan, replan_over_members, validate_member_set

            member_set = validate_member_set(_load(args.memberset))
            if args.mode == "replan":
                result = replan_over_members(raw, member_set)
            else:
                committed = _load(args.plan) if args.plan else run_checked(raw)
                result = check_committed_plan(raw, committed, member_set)
        elif args.command == "recalc":
            result = check_plan(raw, _load(args.plan))
        elif args.command == "advice-replan":
            from .advice_revision import AdviceLedger, replan_advice
            from .forecast_archive import ForecastArchive

            weather_map = _load(args.newweather)
            if not isinstance(weather_map, dict) or not weather_map:
                raise InputError("advice-replan: newweather 必须为非空对象 {plot_id: weather}")
            plot_id = args.plot or sorted(weather_map)[0]
            if plot_id not in weather_map:
                raise InputError("advice-replan: 指定地块不在 newweather 中")
            ledger = (AdviceLedger.from_json(Path(args.ledger).read_text(encoding="utf-8"))
                      if args.ledger else AdviceLedger())
            if args.previous_was_displayed:
                prior = ledger.current(plot_id)
                if prior is not None:
                    ledger.mark_displayed(prior["advice_id"], raw.get("now"))
            result = replan_advice(raw, weather_map[plot_id], plot_id=plot_id,
                                   archive=ForecastArchive(), ledger=ledger,
                                   decision_at=raw.get("now"))
            save_ledger_text = ledger.to_json()
        else:
            save_ledger_text = None
            request = validate_request(raw)
            task = next((t for t in request["tasks"] if t["id"] == args.task), None)
            if task is None or args.worker not in {w["id"] for w in request["workers"]}:
                raise InputError("task/worker: 未找到指定任务或劳动者")
            loaded = _load(args.observations)
            if isinstance(loaded, dict):
                unknown = sorted(set(loaded) - {"caliber", "observations"})
                if unknown:
                    raise InputError(f"observations: 未知字段 {unknown}")
                result = review_observations(task, args.worker, loaded.get("observations"),
                                             caliber=loaded.get("caliber"))
            else:
                result = review_observations(task, args.worker, loaded)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        payloads = []
        if getattr(args, "output", None):
            payloads.append((Path(args.output), encoded))
        else:
            print(encoded, end="")
        if getattr(args, "summary", None):
            if args.command == "coverage":
                from .coverage_status import summarize

                summary_text = summarize(result)
            else:
                summary_text = render_summary(result)
            payloads.append((Path(args.summary), summary_text))
        if getattr(args, "save_ledger", None) and save_ledger_text is not None:
            payloads.append((Path(args.save_ledger), save_ledger_text + "\n"))
        if payloads:
            _atomic_write_all(payloads)
        return 0
    except PlanInterrupted as exc:
        # 计算被取消或超时：不写任何输出文件，只报告进度计数器，绝不留半成品计划。
        print(json.dumps({"error": "interrupted", "reason": exc.reason,
                          "progress": exc.progress}, ensure_ascii=False), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted", "reason": "keyboard_interrupt"}, ensure_ascii=False),
              file=sys.stderr)
        return 130
    except (InputError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
