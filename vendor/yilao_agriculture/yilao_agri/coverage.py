"""半开区间覆盖与保守重采样。

本模块只回答两件事，且只使用天气记录自身声明的区间：

1. 作业区间 ``[start, end)`` 是否被记录完整覆盖，缺口在哪里。
2. 作业区间内的降水暴露是多少（**绝不按作业时长或重叠分钟缩放**）。

约定：

- 所有区间都是半开 ``[start, end)``。两条记录共享端点既不算重叠，也不算缺口。
- 覆盖判定只搬运声明过的区间，不外推、不加宽、不把小时值除以分钟。
- 降水按记录声明的累积值整体计入：作业区间再怎么变短，降水也不会变小。
  代价是跨记录的同一场雨可能被低估，因此另外给出跨记录合计，供调用方做保守判断。
- 本模块不判断个人健康，也不把通过当作现场安全。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

Span = tuple[Any, Any, dict]


def _finite_nonneg(value: Any) -> float | None:
    """只接受有限非负数值；bool 是 int 的子类，不能冒充测量值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")) or result < 0:
        return None
    return result


def coverage_report(spans: Iterable[Span], start: Any, end: Any) -> dict:
    """按半开区间求 ``[start, end)`` 的覆盖片段与缺口。

    ``spans`` 为 ``(a, b, payload)``，表示记录区间 ``[a, b)``；调用方负责保证
    区间有效且互不重叠。返回 ``pieces`` 按时间升序、``gaps`` 为未被覆盖的子区间。
    """
    ordered = sorted(spans, key=lambda item: (item[0], item[1]))
    pieces: list[tuple[Any, Any, dict]] = []
    gaps: list[tuple[Any, Any]] = []
    cursor = start
    for a, b, payload in ordered:
        if b <= start or a >= end:
            # 完全在作业区间之外：既不贡献覆盖，也不贡献缺口。
            continue
        left, right = max(a, start), min(b, end)
        if left > cursor:
            gaps.append((cursor, left))
        cursor = max(cursor, right)
        pieces.append((left, right, payload))
    if cursor < end:
        gaps.append((cursor, end))
    complete = bool(pieces) and not gaps and cursor >= end
    return {
        "complete": complete,
        "pieces": pieces,
        "gaps": gaps,
        "first_gap": gaps[0] if gaps else None,
        "covered_minutes": sum((b - a).total_seconds() / 60.0 for a, b, _ in pieces),
    }


def precipitation_exposure(spans: Iterable[Span], start: Any, end: Any) -> dict:
    """给出作业区间的保守降水暴露，**不做任何按时长的缩放**。

    - ``max_record_mm``：与作业区间相交的各条记录中，最大的单条声明值。
    - ``interval_total_mm``：与作业区间相交的各条记录声明值的直接合计（不摊薄）。
      只要有任一条相交记录缺少有效降水值，合计返回 ``None``，不按 0 补齐。
    - ``scaling_by_interval``：恒为 ``False``；这是本模块对外承诺的不变量。
      单条相交记录无论与作业区间重叠几分钟，都按声明的整段值计入，
      所以区间缩短只可能减少相交记录条数，不会把任何一条记录的降水摊薄。
    """
    overlapping: list[dict] = []
    for a, b, record in spans:
        if b <= start or a >= end:
            continue
        overlapping.append(record)
    values: list[float] = []
    unknown = False
    for record in overlapping:
        value = _finite_nonneg(record.get("precipitation_mm"))
        if value is None:
            unknown = True
        else:
            values.append(value)
    return {
        "records_overlapping": len(overlapping),
        "max_record_mm": max(values) if values else None,
        "interval_total_mm": None if (unknown or not values) else sum(values),
        "unknown_mm": unknown,
        "scaling_by_interval": False,
    }


def accumulation_exceeds(exposure: dict, limit: float) -> bool:
    """跨记录合计是否超过限制；仅在有多条相交记录且合计已知时成立。

    单条记录的比较由调用方按同一限制单独给出，这里补的是「把同一场雨拆成多条
    记录」时不会被放行的保守判定。
    """
    total = exposure.get("interval_total_mm")
    if exposure.get("records_overlapping", 0) <= 1 or total is None:
        return False
    return total > limit
