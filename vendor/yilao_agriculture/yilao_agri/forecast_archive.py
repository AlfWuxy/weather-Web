"""按决策时点归档预报证据并支持原样回放；事后更新只能追加，不得覆盖原预测证据。

本模块只做“存档 / 回放 / 校验”，不评估人体安全，也不替代 ``environment.assess_interval``。

- 归档（archive）：把某个地块当时的天气信封，连同来源、发布时间和决策时点，
  冻结为一条不可变修订（revision）。
- 追加（append-only）：更新的预报只能形成新的修订；同一产品身份（地块 + 来源 +
  类型 + 发布时间）若出现不同内容，视为改写历史证据并直接拒绝。
- 回放（replay）：从某条修订原样重建 weather 信封，可再次喂给
  ``environment`` / ``engine`` 复算，用于核对当时决策。
- 校验（verify）：逐条重算内容哈希与条目哈希，并校验签发、取得、决策的时间顺序。

时间必须显式带时区；所有返回结构都是深拷贝，调用方修改不会影响已存证据。
取得时间不得晚于决策时间。省略 retrieved_at 时沿用调用方决策时间，
并标记 retrieved_at_basis=defaulted_to_decision；显式传入标记 caller_declared。
两种标记都只是调用方提供的时间依据，不认证真实下载时间，也不证明历史可获得性。

证据标签：本模块为 IMPLEMENTATION_TEST 级实现，不构成现场或健康证据。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from typing import Any

from .models import parse_time

ARCHIVE_SCHEMA_VERSION = "1.0"

# 参与“产品身份”的字段：同一身份若内容不同即为改写。
_PRODUCT_FIELDS = ("source", "kind", "issued_at")


class ArchiveError(ValueError):
    """归档失败；``code`` 为稳定的机器可读原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _iso(value: Any, label: str) -> str:
    """规范化为带时区的 ISO 字符串；拒绝无时区或非法时间。"""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ArchiveError("ARCHIVE_TIME_INVALID", f"{label} 缺少时区。")
        value = value.isoformat()
    try:
        return parse_time(value).isoformat()
    except Exception as exc:  # InputError 等
        raise ArchiveError("ARCHIVE_TIME_INVALID", f"{label} 无效或缺少时区：{value!r}。") from exc


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ArchiveError(
            "ARCHIVE_PAYLOAD_NOT_JSON", "归档内容必须是可规范化的 JSON（不含 NaN/Infinity）。"
        ) from exc


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalize_weather(weather: Any) -> dict:
    if not isinstance(weather, dict):
        raise ArchiveError("ARCHIVE_WEATHER_INVALID", "喂给归档的 weather 必须是对象。")
    normalized = deepcopy(weather)
    # 时间字段统一规范化，避免同一时刻的不同写法得到不同哈希。
    normalized["issued_at"] = _iso(normalized.get("issued_at"), "weather.issued_at")
    return normalized


def product_key(plot_id: str, weather: dict) -> dict:
    """返回产品身份：同一预报产品不同内容即视为改写历史。"""
    if not isinstance(plot_id, str) or not plot_id.strip():
        raise ArchiveError("ARCHIVE_PLOT_INVALID", "plot_id 必须为非空字符串。")
    return {
        "plot_id": plot_id,
        "source": weather.get("source"),
        "kind": weather.get("kind"),
        "issued_at": weather.get("issued_at"),
    }


def content_sha256(plot_id: str, weather: dict) -> str:
    """内容寻址哈希：只由地块与完整天气信封决定，与决策时点无关。"""
    return _digest({"plot_id": plot_id, "weather": weather})


def revision_id(plot_id: str, weather: dict) -> str:
    return content_sha256(plot_id, weather)[:16]


def _entry_sha256(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_sha256"}
    return _digest(body)


def _archive_times(issued_at: Any, decision_at: Any, retrieved_at: Any) -> tuple[str, str]:
    """按实际时刻验证来源已签发、已取得，之后才能作出决策。"""
    issued = _iso(issued_at, "weather.issued_at")
    decision = _iso(decision_at, "decision_at")
    retrieved = _iso(retrieved_at, "retrieved_at")
    if parse_time(retrieved) > parse_time(decision):
        raise ArchiveError("ARCHIVE_RETRIEVED_AFTER_DECISION", "取得时间晚于决策时间，不能作为当时证据。")
    if parse_time(issued) > parse_time(retrieved):
        raise ArchiveError("ARCHIVE_ISSUED_AFTER_RETRIEVAL", "签发时间晚于取得时间，时间依据相互矛盾。")
    return decision, retrieved


def build_entry(
    plot_id: str,
    weather: dict,
    decision_at: Any,
    retrieved_at: Any = None,
    attached: Any = None,
    revision_seq: int = 0,
    late_arrival: bool = False,
) -> dict:
    """构造一条修订；缺省取得时间仅沿用决策时间，不认证取得真实性。

    不写入任何存储；``attached`` 可放当时决策证据（如 plan 结果）。
    """
    normalized = _normalize_weather(weather)
    key = product_key(plot_id, normalized)
    decision, retrieved = _archive_times(
        normalized["issued_at"], decision_at,
        retrieved_at if retrieved_at is not None else decision_at,
    )
    entry = {
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "revision_seq": int(revision_seq),
        "revision_id": revision_id(plot_id, normalized),
        "plot_id": plot_id,
        "product": {"source": key["source"], "kind": key["kind"], "issued_at": key["issued_at"]},
        "decision_at": decision,
        "retrieved_at": retrieved,
        "retrieved_at_basis": "defaulted_to_decision" if retrieved_at is None else "caller_declared",
        "late_arrival": bool(late_arrival),
        "weather": normalized,
        "attached": deepcopy(attached),
        "content_sha256": content_sha256(plot_id, normalized),
    }
    entry["entry_sha256"] = _entry_sha256(entry)
    return entry


def verify_entry(entry: Any) -> dict:
    """核对哈希及时间语义；自行重算哈希不能使矛盾时间有效。"""
    errors: list[str] = []
    if not isinstance(entry, dict):
        return {"valid": False, "errors": ["entry 必须为对象"], "revision_id": None}
    plot_id = entry.get("plot_id")
    weather = entry.get("weather")
    if not isinstance(plot_id, str) or not plot_id.strip():
        errors.append("plot_id 缺失或非法")
    if not isinstance(weather, dict):
        errors.append("weather 缺失或非法")
    else:
        try:
            _archive_times(weather.get("issued_at"), entry.get("decision_at"), entry.get("retrieved_at"))
        except ArchiveError as exc:
            errors.append(str(exc))
        if entry.get("product") != {field: weather.get(field) for field in _PRODUCT_FIELDS}:
            errors.append("ARCHIVE_PRODUCT_MISMATCH: product 与 weather 的产品身份不一致")
    if not errors:
        try:
            expected_content = content_sha256(plot_id, weather)
        except ArchiveError as exc:
            expected_content = None
            errors.append(exc.message)
        if expected_content is not None and entry.get("content_sha256") != expected_content:
            errors.append("content_sha256 与当前 weather 不一致：内容已被改动")
        try:
            expected_rev = revision_id(plot_id, weather)
        except ArchiveError:
            expected_rev = None
        if expected_rev is not None and entry.get("revision_id") != expected_rev:
            errors.append("revision_id 与当前内容不一致")
    recorded = entry.get("entry_sha256")
    if not isinstance(recorded, str):
        errors.append("entry_sha256 缺失")
    else:
        try:
            expected_entry = _entry_sha256(entry)
        except ArchiveError as exc:
            errors.append(str(exc))
        else:
            if recorded != expected_entry:
                errors.append("entry_sha256 与当前条目不一致：条目已被改动")
    return {
        "valid": not errors,
        "errors": errors,
        "revision_id": entry.get("revision_id"),
    }


def replay_weather(entry: dict) -> dict:
    """原样回放：返回可直接用作 ``request["weather"]`` 的 ``{plot_id: weather}``。"""
    check = verify_entry(entry)
    if not check["valid"]:
        raise ArchiveError("ARCHIVE_ENTRY_TAMPERED", "；".join(check["errors"]))
    return {entry["plot_id"]: deepcopy(entry["weather"])}


def replay_into_request(entry: dict, request: dict) -> dict:
    """把回放的天气信封替换进请求副本，供复算当时决策；不改原请求。"""
    replayed = replay_weather(entry)
    cloned = deepcopy(request)
    weather_map = cloned.get("weather")
    if not isinstance(weather_map, dict):
        weather_map = {}
        cloned["weather"] = weather_map
    weather_map[entry["plot_id"]] = replayed[entry["plot_id"]]
    return cloned


class ForecastArchive:
    """按地块追加式保存预报修订；已写入的修订不会被后续更新改动。"""

    def __init__(self) -> None:
        self._entries: dict[str, list[dict]] = {}
        self._by_product: dict[tuple, str] = {}   # product identity -> revision_id
        self._by_id: dict[str, dict] = {}          # revision_id -> entry
        self._latest_issued: dict[str, str] = {}   # plot_id -> 最新发布时间
        self._last_decision: dict[str, str] = {}   # plot_id -> 最近一次决策时点

    # -- 写入 -------------------------------------------------------------
    def append(
        self,
        plot_id: str,
        weather: dict,
        decision_at: Any,
        retrieved_at: Any = None,
        attached: Any = None,
    ) -> dict:
        normalized = _normalize_weather(weather)
        key = product_key(plot_id, normalized)
        identity = (key["plot_id"], key["source"], key["kind"], key["issued_at"])
        rev = revision_id(plot_id, normalized)
        # 重复归档也须先校验本次时间声明，不能借幂等返回绕过未来取得检查。
        decision, _ = _archive_times(
            key["issued_at"], decision_at,
            retrieved_at if retrieved_at is not None else decision_at,
        )

        existing_id = self._by_product.get(identity)
        if existing_id is not None and existing_id != rev:
            raise ArchiveError(
                "ARCHIVE_PRODUCT_REWRITE",
                "同一产品身份（地块/来源/类型/发布时间）出现不同内容，"
                "不得覆盖原预测证据；请作为新的发布时间单独归档。",
            )
        if existing_id is not None:
            # 同一内容重复归档：幂等，不新增修订、不改动原证据。
            return deepcopy(self._by_id[existing_id])

        last = self._last_decision.get(plot_id)
        if last is not None and parse_time(decision) < parse_time(last):
            raise ArchiveError(
                "ARCHIVE_DECISION_OUT_OF_ORDER",
                "决策时点早于已归档记录；不得回填/重排历史决策证据。",
            )

        latest = self._latest_issued.get(plot_id)
        late = latest is not None and parse_time(key["issued_at"]) < parse_time(latest)
        seq = len(self._entries.get(plot_id, [])) + 1
        entry = build_entry(
            plot_id, normalized, decision_at, retrieved_at, attached,
            revision_seq=seq, late_arrival=late,
        )
        self._entries.setdefault(plot_id, []).append(entry)
        self._by_product[identity] = entry["revision_id"]
        self._by_id[entry["revision_id"]] = entry
        if latest is None or parse_time(key["issued_at"]) > parse_time(latest):
            self._latest_issued[plot_id] = key["issued_at"]
        if last is None or parse_time(decision) > parse_time(last):
            self._last_decision[plot_id] = decision
        return deepcopy(entry)

    # -- 读取 -------------------------------------------------------------
    def revisions(self, plot_id: str) -> tuple[dict, ...]:
        return tuple(deepcopy(e) for e in self._entries.get(plot_id, ()))

    def latest(self, plot_id: str) -> dict | None:
        """最新发布时间的修订；迟到的旧预报不会顶替它。"""
        entries = self._entries.get(plot_id)
        if not entries:
            return None
        newest = max(entries, key=lambda e: (parse_time(e["product"]["issued_at"]), e["revision_seq"]))
        return deepcopy(newest)

    def as_of(self, plot_id: str, decision_at: Any) -> dict | None:
        """返回查询时刻之前已决策且已取得的修订（同等取最新序列）。"""
        moment = parse_time(_iso(decision_at, "decision_at"))
        picked = None
        for entry in self._entries.get(plot_id, ()):
            decision = parse_time(_iso(entry.get("decision_at"), "decision_at"))
            retrieved = parse_time(_iso(entry.get("retrieved_at"), "retrieved_at"))
            if decision <= moment and retrieved <= moment:
                _archive_times(entry["weather"].get("issued_at"), entry["decision_at"], entry["retrieved_at"])
                if picked is None or (decision, entry["revision_seq"]) > (
                    parse_time(picked["decision_at"]), picked["revision_seq"]
                ):
                    picked = entry
        return deepcopy(picked) if picked is not None else None

    def history(self, plot_id: str) -> tuple[dict, ...]:
        """轻量台账：不给全量 records，只列修订来源信息。"""
        return tuple(
            {
                "revision_seq": e["revision_seq"],
                "revision_id": e["revision_id"],
                "issued_at": e["product"]["issued_at"],
                "source": e["product"]["source"],
                "kind": e["product"]["kind"],
                "decision_at": e["decision_at"],
                "retrieved_at": e["retrieved_at"],
                # 老归档没有依据标记时不推断来源。
                "retrieved_at_basis": e.get("retrieved_at_basis", "legacy_unspecified"),
                "late_arrival": e["late_arrival"],
                "entry_sha256": e["entry_sha256"],
            }
            for e in self._entries.get(plot_id, ())
        )

    def replay(self, plot_id: str, revision_id_: str | None = None,
               decision_at: Any = None) -> dict:
        """按 revision_id 或 decision_at 定位修订并回放；两者都缺则取最新。"""
        if revision_id_ is not None:
            entry = self._by_id.get(revision_id_)
            if entry is None:
                raise ArchiveError("ARCHIVE_REVISION_UNKNOWN", f"未知 revision_id：{revision_id_}。")
        elif decision_at is not None:
            entry = self.as_of(plot_id, decision_at)
            if entry is None:
                raise ArchiveError("ARCHIVE_AS_OF_MISSING", "该决策时点之前没有归档证据。")
        else:
            entry = self.latest(plot_id)
            if entry is None:
                raise ArchiveError("ARCHIVE_EMPTY", f"地块 {plot_id} 尚无归档。")
        if entry["plot_id"] != plot_id:
            raise ArchiveError("ARCHIVE_PLOT_MISMATCH", "revision_id 不属于该地块。")
        return replay_weather(entry)

    # -- 校验 / 持久化 -----------------------------------------------------
    def verify_all(self) -> dict:
        issues = []
        for plot_id, entries in self._entries.items():
            for entry in entries:
                check = verify_entry(entry)
                if not check["valid"]:
                    issues.append({"plot_id": plot_id, "revision_id": entry.get("revision_id"),
                                   "errors": check["errors"]})
        return {"valid": not issues, "revision_count": len(self._by_id), "issues": issues}

    def to_json(self) -> str:
        rows = [e for entries in self._entries.values() for e in entries]
        return json.dumps(
            {"archive_schema_version": ARCHIVE_SCHEMA_VERSION, "entries": rows},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> "ForecastArchive":
        data = json.loads(text)
        archive = cls()
        for entry in data.get("entries", []):
            check = verify_entry(entry)
            if not check["valid"]:
                raise ArchiveError("ARCHIVE_LOAD_TAMPERED", "；".join(check["errors"]))
            archive._restore(entry)
        return archive

    def _restore(self, entry: dict) -> None:
        """仅由 from_json 使用：按原序列恢复且不重算身份冲突。"""
        plot_id = entry["plot_id"]
        key = product_key(plot_id, entry["weather"])
        identity = (key["plot_id"], key["source"], key["kind"], key["issued_at"])
        self._entries.setdefault(plot_id, []).append(entry)
        self._by_product[identity] = entry["revision_id"]
        self._by_id[entry["revision_id"]] = entry
        issued = key["issued_at"]
        latest = self._latest_issued.get(plot_id)
        if latest is None or parse_time(issued) > parse_time(latest):
            self._latest_issued[plot_id] = issued
        decision = entry["decision_at"]
        last = self._last_decision.get(plot_id)
        if last is None or parse_time(decision) > parse_time(last):
            self._last_decision[plot_id] = decision
