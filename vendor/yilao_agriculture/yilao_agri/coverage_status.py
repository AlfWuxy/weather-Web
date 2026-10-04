"""R14-A10 覆盖状态检测：15 类蔬菜 + 5 粮油模块 × 18 环节。

本模块只回答三件事：

1. 目录声明了哪些作物组与环节（V01–V15、G01–G05、01–18）。
2. 已验收运行时对「作物组 × 环节」真正支持到什么程度。
3. 哪些项没有被支持——逐条列出，不得静默丢弃。

约定：

- ``SUPPORTED_RUNTIME``：运行时存在该作物×环节规则；当前为 0，检测器如实报告，不伪造绿色。
- ``CATALOG_ONLY``：目录或别名层有记录，但运行时没有该作物×环节规则，不可推荐。
- ``UNSUPPORTED``：运行时对该组完全没有表示；目录里仍保留该行，供扩展。
- 身份 HOLD（如菜瓜、芝麻、高杆作物）单独挂在对应组上，不外扩成整组结论。
- 本模块只读：不改写输入请求，不判断农艺适用性，不打开推荐，也不构成出工许可或
  个人健康安全结论。某作物是否经过某环节属未核查事实，一律记为 ``applicability=unknown``。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "data" / "coverage" / "coverage_source.json"
ALIAS_CATALOG_PATH = ROOT / "data" / "crops" / "local_alias_catalog.json"

SUPPORTED_RUNTIME = "SUPPORTED_RUNTIME"
CATALOG_ONLY = "CATALOG_ONLY"
IDENTITY_HOLD = "IDENTITY_HOLD"
UNSUPPORTED = "UNSUPPORTED"

# 数值越大表示越「没有被支持」，用于把作物组与环节两维合成单元格状态。
SEVERITY = {
    SUPPORTED_RUNTIME: 0,
    CATALOG_ONLY: 1,
    IDENTITY_HOLD: 2,
    UNSUPPORTED: 3,
}
VISIBLE_NON_SUPPORTED = frozenset({CATALOG_ONLY, IDENTITY_HOLD, UNSUPPORTED})

_GROUP_PATTERN = re.compile(r"([VG]\d{2})")


class CoverageError(ValueError):
    """覆盖状态目录或查询错误；code 供调用方定位。"""

    def __init__(self, code, message, field=""):
        self.code = code
        self.message = message
        self.field = field
        prefix = f"{field}: " if field else ""
        super().__init__(f"{prefix}{code}: {message}")


def load_source(path=None) -> dict:
    """读取覆盖声明；path 可为 dict（测试夹具）。"""
    if isinstance(path, dict):
        source = deepcopy(path)
    else:
        target = Path(path) if path else SOURCE_PATH
        if not target.is_file():
            raise CoverageError("SOURCE_MISSING", f"找不到覆盖声明：{target}")
        try:
            source = json.loads(target.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CoverageError("SOURCE_BAD_JSON", f"覆盖声明不是合法 JSON：{exc}") from None
    _validate_source(source)
    return source


def _validate_source(source):
    if not isinstance(source, dict):
        raise CoverageError("BAD_TYPE", "覆盖声明必须为对象")
    groups = source.get("crop_groups")
    stages = source.get("stages")
    if not isinstance(groups, list) or not groups:
        raise CoverageError("BAD_TYPE", "crop_groups 必须为非空数组", "crop_groups")
    if not isinstance(stages, list) or not stages:
        raise CoverageError("BAD_TYPE", "stages 必须为非空数组", "stages")
    seen = set()
    for i, row in enumerate(groups):
        if not isinstance(row, dict) or not str(row.get("id") or "").strip():
            raise CoverageError("BAD_TYPE", "作物组必须含 id", f"crop_groups[{i}]")
        gid = row["id"]
        if gid in seen:
            raise CoverageError("DUPLICATE_ID", f"重复作物组 {gid}", f"crop_groups[{i}]")
        seen.add(gid)
        if row.get("kind") not in ("vegetable", "grain_oil"):
            raise CoverageError("BAD_TYPE", "kind 必须为 vegetable 或 grain_oil", f"crop_groups[{i}].kind")
        if not _GROUP_PATTERN.fullmatch(gid):
            raise CoverageError("BAD_ID", f"作物组 id 必须形如 V01/G01：{gid}", f"crop_groups[{i}].id")
    seen_stages = set()
    for i, row in enumerate(stages):
        if not isinstance(row, dict) or not str(row.get("id") or "").strip():
            raise CoverageError("BAD_TYPE", "环节必须含 id", f"stages[{i}]")
        sid = row["id"]
        if sid in seen_stages:
            raise CoverageError("DUPLICATE_ID", f"重复环节 {sid}", f"stages[{i}]")
        seen_stages.add(sid)
    mapping = source.get("alias_id_to_group")
    if not isinstance(mapping, dict):
        raise CoverageError("BAD_TYPE", "alias_id_to_group 必须为对象", "alias_id_to_group")
    for key, value in mapping.items():
        if not _GROUP_PATTERN.fullmatch(str(value or "")):
            raise CoverageError("BAD_ID", f"别名 {key} 指向非法作物组 {value}", f"alias_id_to_group.{key}")
    allow = source.get("alias_identity_hold_statuses")
    if not isinstance(allow, list) or not allow:
        raise CoverageError("BAD_TYPE", "alias_identity_hold_statuses 必须为非空数组", "alias_identity_hold_statuses")


def load_alias_catalog(path=None) -> dict:
    """读取已验收的作物别名目录；path 可为 dict。"""
    if isinstance(path, dict):
        catalog = deepcopy(path)
    else:
        target = Path(path) if path else ALIAS_CATALOG_PATH
        if not target.is_file():
            return {"aliases": []}
        catalog = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("aliases", []), list):
        raise CoverageError("BAD_TYPE", "别名目录必须含 aliases 数组")
    return catalog


def _group_of(alias_row, mapping):
    """先查显式映射，再回退按 alias_id / target_crop_id 里的 V##/G## 取组。"""
    alias_id = alias_row.get("alias_id")
    if alias_id in mapping:
        return mapping[alias_id]
    for text in (alias_id or "", alias_row.get("target_crop_id") or ""):
        found = _GROUP_PATTERN.search(str(text))
        if found:
            return found.group(1)
    return None


def _runtime_groups(source):
    facts = source.get("runtime_facts") or {}
    groups = facts.get("per_crop_stage_rule_groups") or []
    return {g for g in groups if isinstance(g, str)}


def audit_aliases(source, alias_catalog):
    """把别名行归到作物组，并保留无法归组/指向未知组的行（可见，不静默丢）。"""
    mapping = source["alias_id_to_group"]
    known = {row["id"] for row in source["crop_groups"]}
    assigned: dict[str, list] = {}
    unmapped: list = []
    unknown_group: list = []
    hold_statuses = set(source["alias_identity_hold_statuses"])
    for row in alias_catalog.get("aliases", []):
        gid = _group_of(row, mapping)
        entry = {
            "alias_id": row.get("alias_id"),
            "surface_form": row.get("surface_form"),
            "region_id": row.get("region_id"),
            "status": row.get("status"),
            "candidates": len(row.get("candidates") or []),
            "local_confirmed": bool(row.get("local_confirmed")),
        }
        if gid is None:
            unmapped.append(entry)
            continue
        if gid not in known:
            entry["group_id"] = gid
            unknown_group.append(entry)
            continue
        if row.get("status") in hold_statuses:
            entry["identity_hold"] = True
        assigned.setdefault(gid, []).append(entry)
    return assigned, unmapped, unknown_group


def _group_status(group_id, rows, runtime_groups):
    if group_id in runtime_groups:
        return SUPPORTED_RUNTIME
    if not rows:
        return UNSUPPORTED
    return CATALOG_ONLY


def _stage_status(stage_id, source):
    facts = source.get("runtime_facts") or {}
    if stage_id in set(facts.get("per_stage_rule_ids") or []):
        return SUPPORTED_RUNTIME
    return CATALOG_ONLY


def build_matrix(source=None, alias_catalog=None) -> dict:
    """生成 20 组 × 18 环节的覆盖状态表；未支持项逐条可见。"""
    source = load_source(source)
    alias_catalog = load_alias_catalog(alias_catalog)
    runtime_groups = _runtime_groups(source)
    assigned, unmapped, unknown_group = audit_aliases(source, alias_catalog)

    groups = []
    for row in source["crop_groups"]:
        gid = row["id"]
        rows = assigned.get(gid, [])
        hold_aliases = [e["alias_id"] for e in rows if e.get("identity_hold")]
        groups.append({
            "id": gid,
            "kind": row["kind"],
            "name_zh": row.get("name_zh", ""),
            "responsibility": row.get("responsibility", ""),
            "status": _group_status(gid, rows, runtime_groups),
            "recommendation_enabled": False,
            "alias_rows": len(rows),
            "identity_hold": bool(hold_aliases),
            "identity_hold_aliases": hold_aliases,
            "local_confirmed": any(e["local_confirmed"] for e in rows),
            "reason": _group_reason(gid, rows, runtime_groups, hold_aliases),
        })

    stages = []
    for row in source["stages"]:
        sid = row["id"]
        stages.append({
            "id": sid,
            "name_zh": row.get("name_zh", ""),
            "responsibility": row.get("responsibility", ""),
            "status": _stage_status(sid, source),
            "runtime_stage_logic": _stage_status(sid, source) == SUPPORTED_RUNTIME,
            "reason": "运行时按通用规则处理，未按环节分派专属规则；目录已声明" ,
        })

    group_index = {g["id"]: g for g in groups}
    cells = []
    for group in groups:
        for stage in stages:
            status = max(
                (group["status"], stage["status"]),
                key=lambda item: SEVERITY[item],
            )
            cells.append({
                "crop_group": group["id"],
                "stage": stage["id"],
                "status": status,
                "recommendation_enabled": False,
                "identity_hold_aliases": list(group["identity_hold_aliases"]),
                "applicability": (source.get("applicability") or {}).get("value", "unknown"),
            })

    summary = {
        "groups_total": len(groups),
        "vegetable_groups": sum(1 for g in groups if g["kind"] == "vegetable"),
        "grain_oil_groups": sum(1 for g in groups if g["kind"] == "grain_oil"),
        "stages_total": len(stages),
        "cells_total": len(cells),
        "cells_by_status": _count_by(cells, "status"),
        "groups_by_status": _count_by(groups, "status"),
        "groups_with_identity_hold": sorted(g["id"] for g in groups if g["identity_hold"]),
        "supported_runtime_cells": sum(1 for c in cells if c["status"] == SUPPORTED_RUNTIME),
    }

    matrix = {
        "schema_id": source.get("schema_id"),
        "contract_version": source.get("contract_version"),
        "task_id": source.get("task_id"),
        "accepted_source_hash": source.get("accepted_source_hash"),
        "note": source.get("note"),
        "generated_from": {
            "coverage_source": str(SOURCE_PATH.relative_to(ROOT)),
            "alias_catalog": str(ALIAS_CATALOG_PATH.relative_to(ROOT)),
        },
        "groups": groups,
        "stages": stages,
        "cells": cells,
        "summary": summary,
        "alias_audit": {
            "unmapped": unmapped,
            "unknown_group": unknown_group,
            "mapped_alias_rows": sum(len(v) for v in assigned.values()),
        },
        "visible_unsupported": visible_unsupported({"groups": groups, "stages": stages, "cells": cells}),
        "completeness": detect_gaps({"groups": groups, "stages": stages, "cells": cells}, source),
    }
    return matrix


def _group_reason(group_id, rows, runtime_groups, hold_aliases):
    if group_id in runtime_groups:
        return "运行时存在该组规则；当前声明为有规则"
    if not rows:
        return "运行时无该组别名记录，也无作物×环节规则；目录保留该行待扩展"
    reason = f"仅别名层有 {len(rows)} 行记录，无作物×环节规则，不可推荐"
    if hold_aliases:
        reason += "；含身份 HOLD 别名：" + "、".join(hold_aliases)
    return reason


def _count_by(rows, key):
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return dict(sorted(counts.items()))


def visible_unsupported(matrix) -> dict:
    """把所有未被运行时支持的项逐条列出，避免「没支持」被静默省略。"""
    groups = [g for g in matrix["groups"] if g["status"] != SUPPORTED_RUNTIME]
    stages = [s for s in matrix.get("stages", []) if s["status"] != SUPPORTED_RUNTIME]
    cells = [c for c in matrix["cells"] if c["status"] != SUPPORTED_RUNTIME]
    return {
        "groups": [{"id": g["id"], "name_zh": g["name_zh"], "status": g["status"]} for g in groups],
        "stages": [{"id": s["id"], "name_zh": s["name_zh"], "status": s["status"]} for s in stages],
        "cells_total": len(cells),
        "cells_by_status": _count_by(cells, "status"),
    }


def detect_gaps(matrix, source=None) -> dict:
    """核对声明与产物：声明过的组/环节若不在表里，必须报缺，不得当作已覆盖。"""
    if source is None:
        source = load_source()
    declared_groups = [row["id"] for row in source["crop_groups"]]
    declared_stages = [row["id"] for row in source["stages"]]
    present_groups = [g["id"] for g in matrix["groups"]]
    present_stages = [s["id"] for s in matrix["stages"]]
    missing_groups = [g for g in declared_groups if g not in set(present_groups)]
    missing_stages = [s for s in declared_stages if s not in set(present_stages)]
    extra_groups = [g for g in present_groups if g not in set(declared_groups)]
    extra_stages = [s for s in present_stages if s not in set(declared_stages)]
    cell_pairs = {(c["crop_group"], c["stage"]) for c in matrix["cells"]}
    expected_pairs = {(g, s) for g in present_groups for s in present_stages}
    missing_cells = sorted(expected_pairs - cell_pairs)
    return {
        "declared_groups": len(declared_groups),
        "declared_stages": len(declared_stages),
        "present_groups": len(present_groups),
        "present_stages": len(present_stages),
        "missing_declared_groups": missing_groups,
        "missing_declared_stages": missing_stages,
        "extra_groups_not_declared": extra_groups,
        "extra_stages_not_declared": extra_stages,
        "missing_cells": [f"{g}x{s}" for g, s in missing_cells],
        "complete": not (missing_groups or missing_stages or extra_groups or extra_stages or missing_cells),
    }


def request_coverage(matrix, request) -> dict:
    """核对一个请求的作物/环节是否落在覆盖表内；未覆盖项逐条可见，且不改写请求。"""
    if not isinstance(request, dict):
        raise CoverageError("BAD_TYPE", "请求必须为对象")
    catalog = _alias_lookup()
    group_by_surface = {row["surface"]: row["group"] for row in catalog}
    stage_ids = {s["id"] for s in matrix["stages"]}
    stage_names = {s["name_zh"]: s["id"] for s in matrix["stages"]}
    group_status = {g["id"]: g["status"] for g in matrix["groups"]}
    tasks = request.get("tasks") or []
    findings = []
    for task in tasks:
        crop_id = str(task.get("crop_id") or "")
        stage = str(task.get("stage") or "")
        group = group_by_surface.get(crop_id.casefold())
        if group is None:
            found = _GROUP_PATTERN.search(crop_id)
            group = found.group(1) if found else None
        stage_id = stage if stage in stage_ids else stage_names.get(stage)
        status = group_status.get(group) if group else None
        reasons = []
        if group is None:
            reasons.append(f"作物 {crop_id!r} 未登记在别名目录，也未映射到作物组：标记为目录外，视为未支持")
        elif status != SUPPORTED_RUNTIME:
            reasons.append(f"作物组 {group} 的运行时状态为 {status}：不可推荐")
        if stage_id is None:
            reasons.append(f"环节 {stage!r} 不是 01–18 的编号或正式名：标记为目录外，视为未支持")
        else:
            reasons.append(f"环节 {stage_id} 运行时无专属规则（通用处理）")
        findings.append({
            "task_id": task.get("id"),
            "crop_id": crop_id,
            "crop_group": group,
            "group_status": status,
            "stage_input": stage,
            "stage_id": stage_id,
            "covered": bool(group and stage_id and status == SUPPORTED_RUNTIME),
            "reasons": reasons,
        })
    return {
        "tasks_total": len(findings),
        "tasks_covered": sum(1 for f in findings if f["covered"]),
        "tasks_unsupported": sum(1 for f in findings if not f["covered"]),
        "findings": findings,
        "meaning": "只核对请求的作物/环节是否在覆盖表内；不代表农艺适用、来源已核实或健康安全",
    }


def _alias_lookup():
    catalog = load_alias_catalog()
    source = load_source()
    mapping = source["alias_id_to_group"]
    rows = []
    for row in catalog.get("aliases", []):
        surface = row.get("surface_form")
        gid = _group_of(row, mapping)
        if isinstance(surface, str) and surface.strip() and gid:
            rows.append({"surface": surface.strip().casefold(), "group": gid})
    return rows


def summarize(matrix) -> str:
    s = matrix["summary"]
    lines = [
        "# 覆盖状态表（作物组 × 环节）",
        "",
        f"作物组 {s['groups_total']}（蔬菜 {s['vegetable_groups']} + 粮油 {s['grain_oil_groups']}）；环节 {s['stages_total']}；单元格 {s['cells_total']}。",
        f"单元格状态：{s['cells_by_status']}。",
        f"运行时支持单元格：{s['supported_runtime_cells']}。",
        f"含身份 HOLD 的组：{s['groups_with_identity_hold']}。",
        "",
        "## 未被运行时支持的项（逐条可见）",
        "",
        "| 作物组 | 名称 | 状态 |",
        "|---|---|---|",
    ]
    for g in matrix["visible_unsupported"]["groups"]:
        lines.append(f"| {g['id']} | {g['name_zh']} | {g['status']} |")
    lines += ["", "| 环节 | 名称 | 状态 |", "|---|---|---|"]
    for st in matrix["visible_unsupported"]["stages"]:
        lines.append(f"| {st['id']} | {st['name_zh']} | {st['status']} |")
    comp = matrix["completeness"]
    lines += ["", f"完整性：{'通过' if comp['complete'] else '缺项'}；缺声明组 {comp['missing_declared_groups']}，缺声明环节 {comp['missing_declared_stages']}。", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="宜老农业：作物×环节覆盖状态表（只读）")
    parser.add_argument("--request", help="可选：核对某请求的作物/环节是否在覆盖表内")
    parser.add_argument("--output", help="输出 JSON 路径；缺省打印到标准输出")
    parser.add_argument("--summary", help="输出中文摘要 Markdown 路径")
    args = parser.parse_args(argv)
    try:
        matrix = build_matrix()
        result = matrix
        if args.request:
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            result = dict(matrix)
            result["request_coverage"] = request_coverage(matrix, request)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded, encoding="utf-8")
        else:
            print(encoded, end="")
        if args.summary:
            path = Path(args.summary)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(summarize(matrix), encoding="utf-8")
        return 0
    except (CoverageError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
