"""高风险农事操作边界（R14-A09）。

喷药（`pesticide_application`）与粪肥（`manure_handling`）相关任务只按**显式审核范围**
开放：任务必须带 `operation_scope`，且其覆盖的类别、审核状态与来源都可采用。
任何夹带剂量、稀释、商品名、安全间隔期等用药请求的任务一律关闭——本系统只做识别、
记录与转介，不生成剂量、商品名或施药许可。

本模块只读任务字典，不修改输入。判定为“通过”只表示满足输入中声明的审核范围，
不代表农艺正确、现场有效、个人健康安全或疾病概率，也不替代当地植保/农技签发。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "HIGH_RISK_CLASSES",
    "PESTICIDE_TOKENS",
    "MANURE_TOKENS",
    "DOSAGE_TOKENS",
    "boundary_statement",
    "classify_operation",
    "dosage_request",
    "check_operation_scope",
]

# 需要显式审核范围才开放的高风险作业类别。
HIGH_RISK_CLASSES = ("pesticide_application", "manure_handling")

# 喷药/化学防治：只匹配明确的化学作业词，不含“巡田/摘除/物理防治”等非化学环节。
PESTICIDE_TOKENS = (
    "pesticide", "pesticide_application", "apply_pesticide", "chemical_control",
    "spray", "spraying", "insecticide", "fungicide", "herbicide", "acaricide",
    "rodenticide",
    "农药", "喷药", "施药", "打药", "洒药", "喷施", "杀虫剂", "杀菌剂",
    "除草剂", "杀螨剂", "药剂",
)

# 粪肥/有机肥相关：粪肥与化肥不合并；此处只锁粪肥与农家有机物料族。
MANURE_TOKENS = (
    "manure", "manure_carry", "farmyard_manure", "organic_fertilizer",
    "human_waste", "slurry", "dung",
    "粪", "粪肥", "挑粪", "人粪", "畜粪", "禽粪", "厩肥", "农家肥",
    "有机肥", "粪水",
)

# 剂量/用药请求：出现即关闭，与是否已审核无关。系统不生成剂量或施药许可。
DOSAGE_TOKENS = (
    "dosage", "dose", "dilution", "dilute", "active_ingredient", "trade_name",
    "brand_name", "rei_hours", "phi_days", "ml_per", "per_barrel", "kg_per_ha",
    "剂量", "用量", "稀释", "配比", "浓度", "每桶", "每背", "每亩用",
    "用药量", "施用量", "商品名", "药名", "安全间隔期",
)

_UNKNOWN = {
    "", "unknown", "unverified", "none", "null", "tbd", "todo", "n/a", "na",
    "to be determined", "to be confirmed", "not available", "unspecified",
    "待确认", "待核实", "未知", "待定", "待补充", "待填写",
}


def _known(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return " ".join(value.split()).casefold() not in _UNKNOWN
    return True


def _source(value: Any) -> bool:
    return isinstance(value, str) and _known(value)


def _blob(task: Any) -> str:
    if not isinstance(task, dict):
        return ""
    parts = [str(task.get("operation") or ""), str(task.get("method") or "")]
    tags = task.get("tags")
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags)
    return " ".join(parts).casefold().replace("-", "_")


def _match(blob: str, tokens: tuple[str, ...]) -> bool:
    for token in tokens:
        probe = token.casefold()
        if any("\u4e00" <= ch <= "\u9fff" for ch in probe):
            if probe in blob:
                return True
        elif probe in blob:
            return True
    return False


def classify_operation(task: Any) -> tuple[str, ...]:
    """按 operation/method/tags 归入高风险类别；不含剂量语义，不猜测。"""
    blob = _blob(task)
    if not blob:
        return ()
    classes = []
    if _match(blob, PESTICIDE_TOKENS):
        classes.append("pesticide_application")
    if _match(blob, MANURE_TOKENS):
        classes.append("manure_handling")
    return tuple(classes)


_DOSAGE_FIELDS = ("dosage", "dose", "dilution", "dosage_per_area", "product_name",
                  "active_ingredient", "rei_hours", "phi_days")


def dosage_request(task: Any) -> bool:
    """任务是否夹带剂量/用药请求：显式字段或 operation/method/tags 文本。"""
    if not isinstance(task, dict):
        return False
    for key in _DOSAGE_FIELDS:
        if key in task and task.get(key) not in (None, "", [], {}):
            return True
    return _match(_blob(task), DOSAGE_TOKENS)


def boundary_statement() -> str:
    """对外边界声明：系统只识别、记录、转介，不生成剂量或施药许可。"""
    return ("喷药与粪肥相关任务只按声明的审核范围开放；本系统不生成剂量、稀释倍数、"
            "商品名、安全间隔期或施药许可，只做识别、记录与转介，不代表现场有效或个人安全。")


def _blocked(classes: tuple[str, ...], code: str, message: str, required: bool = True) -> dict:
    return {"required": required, "classes": list(classes), "blocked": True,
            "code": code, "message": message}


def check_operation_scope(task: Any, mode: str) -> dict:
    """高风险操作边界检查。

    返回 ``{required, classes, blocked, code, message[, scope]}``。规则：

    1. 夹带剂量/用药请求 → 一律关闭（``DOSAGE_PERMISSION_WITHHELD``），无论是否已审核。
    2. 非高风险任务且无剂量请求 → 不介入（``blocked=False``）。
    3. 高风险任务缺少/无效的 ``operation_scope`` → 关闭，不按“默认开放”放行。
    4. 审核范围须覆盖全部高风险类别、审核状态可采用、来源可辨识。
    """
    classes = classify_operation(task)
    if dosage_request(task):
        return _blocked(classes, "DOSAGE_PERMISSION_WITHHELD",
                        "任务夹带剂量/用药请求；系统不生成剂量或施药许可，只识别、记录与转介。")
    if not classes:
        return {"required": False, "classes": [], "blocked": False, "code": None, "message": None}

    accepted = {"confirmed", "illustrative"} if mode == "demonstration" else {"confirmed"}
    scope = task.get("operation_scope") if isinstance(task, dict) else None
    label = "、".join(classes)
    if not isinstance(scope, dict):
        return _blocked(classes, "OPERATION_SCOPE_UNREVIEWED",
                        f"高风险操作（{label}）缺少显式审核范围，未按已审核范围开放。")
    declared = scope.get("classes")
    if not isinstance(declared, list) or not all(isinstance(c, str) and c.strip() for c in declared):
        return _blocked(classes, "OPERATION_SCOPE_CLASSES_INVALID",
                        "审核范围未列出有效的作业类别，不能视为已开放。")
    missing = sorted(set(classes) - set(declared))
    if missing:
        return _blocked(classes, "OPERATION_SCOPE_CLASSES_MISMATCH",
                        f"审核范围未覆盖高风险类别：{'、'.join(missing)}。")
    if scope.get("review_status") not in accepted:
        return _blocked(classes, "OPERATION_SCOPE_UNREVIEWED",
                        f"审核范围状态为 {scope.get('review_status')!r}，当前模式不可采用。")
    if not _source(scope.get("source")):
        return _blocked(classes, "OPERATION_SCOPE_SOURCE_UNKNOWN",
                        "审核范围缺少可辨识来源，占位或空来源不算已审核。")
    return {"required": True, "classes": list(classes), "blocked": False, "code": None, "message": None,
            "scope": {"classes": list(declared), "review_status": scope.get("review_status"),
                      "source": scope.get("source")}}
