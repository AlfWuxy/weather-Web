"""模型/参数版本注册表：候选评估后提升与回退；回退不改硬约束。

证据状态：SYNTHETIC / IMPLEMENTATION_TEST。只管理“模型/参数字典”的版本元数据，
不参与排程搜索、不改天气/人员/负重的任何硬约束，也不证明个人健康安全或现场效果。

本模块落实三条不变量：

1. 提升需先评估。候选只有在 `evaluate` 产出指标、且 `promote` 的验收函数对该指标返回
   真值后，才能成为当前版本；未评估或验收不过一律拒绝。
2. 回退不改硬约束。硬约束以构造时冻结的指纹绑定；版本的 `params` 只允许承载可调旋钮，
   任何试图把硬约束代码写进 `params` 的候选在登记阶段即被拒绝；`rollback` 只换回
   `params`，冻结的硬约束指纹在整个生命周期内保持不变。
3. 旧模型与新数据不得混淆。每个版本绑定登记时的数据指纹；用不同数据指纹组合该版本
   一律报错，须显式登记并评估一个面向新数据的候选，避免把旧模型套到新数据上。
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

# 硬约束代码清单：这些是排程输出的全局硬约束（见 docs/接口约定.md 必测边界，
# 由 audit.verify_plan 复查）。它们不是模型可调参数，任何版本都不得覆盖。
HARD_CONSTRAINT_CODES = frozenset({
    "mode_binding",
    "weather_coverage_and_freshness",
    "hazard_blocking",
    "daylight_required",
    "worker_state_unknown_stop",
    "load_limit_kg",
    "forbidden_tags",
    "resource_capacity",
    "min_rest_and_daily_cap",
    "deadline_and_dependency",
    "agronomy_preconditions",
    "distinct_evidence_kinds",
})

REGISTRY_SCHEMA = "yilao.model_registry.v1"

# 版本 params 里禁止出现的键：硬约束代码本身 + 硬约束载体键。
RESERVED_PARAM_KEYS = frozenset(HARD_CONSTRAINT_CODES) | frozenset({
    "hard_constraints", "hard_constraint_fingerprint",
})

# 版本状态机（单向，除 rollback 激活旧版本外）。
_STATUS_CANDIDATE = "candidate"
_STATUS_EVALUATED = "evaluated"
_STATUS_PROMOTED = "promoted"
_STATUS_RETIRED = "retired"


class ModelRegistryError(ValueError):
    """注册表错误的基类；消息含版本 id 与字段路径。"""


class UnknownVersion(ModelRegistryError):
    """引用了未登记的版本 id。"""


class DuplicateVersion(ModelRegistryError):
    """版本 id 已存在；不允许原地改写历史版本。"""


class HardConstraintDrift(ModelRegistryError):
    """候选试图携带或改写硬约束；版本不得改变硬约束。"""


class VersionDataMismatch(ModelRegistryError):
    """用旧模型版本组合了新数据指纹；必须先登记并评估面向新数据的候选。"""


class PromotionBlocked(ModelRegistryError):
    """提升被拒：未评估、或验收未通过。"""


class RollbackBlocked(ModelRegistryError):
    """回退被拒：目标版本不可回退，或硬约束指纹与冻结值不一致。"""


def canonical_bytes(value: Any) -> bytes:
    """稳定序列化：键排序、无多余空白、UTF-8。用于一切指纹计算。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def fingerprint(value: Any) -> str:
    """对任意 JSON 兼容值取稳定 sha256。"""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def hard_constraint_fingerprint(codes: Iterable[str]) -> str:
    """由硬约束代码集合得到冻结指纹；与注册表 schema 绑定。"""
    return fingerprint({"schema": REGISTRY_SCHEMA,
                        "hard_constraints": sorted(set(codes))})


def data_fingerprint_for_request(raw: Mapping[str, Any]) -> str:
    """复用 R11-A09 的规范化摘要，作为数据指纹；不修改传入对象。"""
    from .models import request_digest
    return request_digest(raw)


@dataclass
class ModelVersion:
    """一个不可改写的模型版本记录。"""

    version_id: str
    algorithm_version: str
    params: Mapping[str, Any]
    data_fingerprint: str
    hard_constraint_fingerprint: str
    metrics: dict | None = None
    status: str = _STATUS_CANDIDATE
    note: str | None = None

    def params_digest(self) -> str:
        return fingerprint(self.params)

    def snapshot(self) -> dict:
        return {
            "version_id": self.version_id,
            "algorithm_version": self.algorithm_version,
            "params": deepcopy(dict(self.params)),
            "params_digest": self.params_digest(),
            "data_fingerprint": self.data_fingerprint,
            "hard_constraint_fingerprint": self.hard_constraint_fingerprint,
            "metrics": deepcopy(self.metrics),
            "status": self.status,
            "note": self.note,
        }


class ModelRegistry:
    """模型/参数版本注册表。

    `hard_constraints` 在构造时冻结；此后任何版本、任何回退都不得改变它。
    """

    def __init__(self, hard_constraints: Iterable[str] | None = None):
        codes = HARD_CONSTRAINT_CODES if hard_constraints is None else hard_constraints
        codes = frozenset(codes)
        unknown = codes - HARD_CONSTRAINT_CODES
        if unknown:
            raise HardConstraintDrift(
                f"未知硬约束代码：{sorted(unknown)}；不得凭空增删硬约束")
        self._hard_constraints = codes
        self._hard_fp = hard_constraint_fingerprint(codes)
        self._versions: dict[str, ModelVersion] = {}
        self._active: str | None = None
        self._events: list[dict] = []

    # ---- 只读视图 ----
    @property
    def hard_constraints(self) -> frozenset:
        return self._hard_constraints

    @property
    def hard_constraint_fingerprint(self) -> str:
        return self._hard_fp

    def active_version_id(self) -> str | None:
        return self._active

    def active(self) -> ModelVersion:
        if self._active is None:
            raise UnknownVersion("尚无当前版本：请先登记、评估并提升一个候选")
        return self._versions[self._active]

    def get(self, version_id: str) -> ModelVersion:
        if version_id not in self._versions:
            raise UnknownVersion(f"未登记的版本：{version_id!r}")
        return self._versions[version_id]

    def history(self) -> list[dict]:
        return deepcopy(self._events)

    def _log(self, action: str, **fields) -> None:
        entry = {"action": action, "hard_constraint_fingerprint": self._hard_fp}
        entry.update(fields)
        self._events.append(entry)

    # ---- 版本生命周期 ----
    def register_candidate(self, version_id: str, algorithm_version: str,
                           params: Mapping[str, Any], data_fingerprint: str,
                           *, hard_constraints: Iterable[str] | None = None,
                           note: str | None = None) -> ModelVersion:
        """登记候选版本。硬约束必须与冻结值一致，且 params 不得夹带硬约束键。"""
        if not version_id:
            raise ModelRegistryError("version_id 不能为空")
        if version_id in self._versions:
            raise DuplicateVersion(f"版本已存在，历史不可就地改写：{version_id!r}")
        if not isinstance(params, Mapping):
            raise ModelRegistryError(f"{version_id!r}.params 必须是映射")
        reserved = RESERVED_PARAM_KEYS.intersection(params)
        if reserved:
            raise HardConstraintDrift(
                f"{version_id!r}.params 含硬约束键 {sorted(reserved)}；"
                "版本参数不得改变硬约束，请用独立流程处理硬约束变更")
        if hard_constraints is not None:
            fp = hard_constraint_fingerprint(hard_constraints)
            if fp != self._hard_fp:
                raise HardConstraintDrift(
                    f"{version_id!r} 声明的硬约束指纹 {fp[:12]}… 与冻结值 "
                    f"{self._hard_fp[:12]}… 不一致；版本不得改变硬约束")
        if not data_fingerprint:
            raise ModelRegistryError(f"{version_id!r}.data_fingerprint 不能为空")
        version = ModelVersion(
            version_id=version_id, algorithm_version=algorithm_version,
            params=deepcopy(dict(params)), data_fingerprint=data_fingerprint,
            hard_constraint_fingerprint=self._hard_fp, note=note)
        self._versions[version_id] = version
        self._log("register", version_id=version_id,
                  algorithm_version=algorithm_version,
                  params_digest=version.params_digest(),
                  data_fingerprint=data_fingerprint)
        return version

    def evaluate(self, version_id: str,
                 evaluator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
                 *, data_fingerprint: str | None = None) -> dict:
        """用评估器对候选打分。评估器只在候选自己的数据指纹上运行。"""
        version = self.get(version_id)
        if version.status != _STATUS_CANDIDATE:
            raise ModelRegistryError(
                f"{version_id!r} 状态为 {version.status!r}，只能评估候选")
        if data_fingerprint is not None and data_fingerprint != version.data_fingerprint:
            raise VersionDataMismatch(
                f"{version_id!r} 绑定数据 {version.data_fingerprint[:12]}…，"
                f"拒绝在 {data_fingerprint[:12]}… 上评估；请登记面向新数据的候选")
        metrics = evaluator(deepcopy(dict(version.params)))
        if not isinstance(metrics, Mapping):
            raise ModelRegistryError("评估器必须返回映射，不能为 None 或标量")
        version.metrics = deepcopy(dict(metrics))
        version.metrics["_evaluated_data_fingerprint"] = version.data_fingerprint
        version.status = _STATUS_EVALUATED
        self._log("evaluate", version_id=version_id,
                  metrics_digest=fingerprint(version.metrics))
        return deepcopy(version.metrics)

    def promote(self, version_id: str,
                acceptance: Callable[[Mapping[str, Any]], bool]) -> ModelVersion:
        """验收通过才提升为当前版本；未评估或验收不过一律拒绝。"""
        version = self.get(version_id)
        if version.status != _STATUS_EVALUATED:
            raise PromotionBlocked(
                f"{version_id!r} 未评估（状态 {version.status!r}）；先评估再提升")
        passed = bool(acceptance(deepcopy(dict(version.metrics or {}))))
        if not passed:
            self._log("promote_rejected", version_id=version_id, reason="acceptance")
            raise PromotionBlocked(f"{version_id!r} 未通过验收，不得提升")
        previous = self._active
        if previous is not None and previous != version_id:
            self._versions[previous].status = _STATUS_RETIRED
        version.status = _STATUS_PROMOTED
        self._active = version_id
        self._log("promote", version_id=version_id, from_version=previous)
        return version

    def rollback(self, target_version_id: str) -> ModelVersion:
        """回退到已提升/已退役版本；只换回 params，硬约束指纹保持不变。"""
        target = self.get(target_version_id)
        if target.status not in (_STATUS_PROMOTED, _STATUS_RETIRED):
            raise RollbackBlocked(
                f"{target_version_id!r} 状态 {target.status!r} 不可回退；"
                "只能回退到曾提升过的版本")
        if target.hard_constraint_fingerprint != self._hard_fp:
            raise RollbackBlocked(
                f"{target_version_id!r} 硬约束指纹与冻结值不一致，拒绝回退")
        previous = self._active
        if previous == target_version_id:
            raise RollbackBlocked(f"{target_version_id!r} 已是当前版本")
        if previous is not None:
            self._versions[previous].status = _STATUS_RETIRED
        target.status = _STATUS_PROMOTED
        self._active = target_version_id
        self._log("rollback", version_id=target_version_id, from_version=previous)
        return target

    # ---- 旧模型 / 新数据绑定 ----
    def bind_data(self, version_id: str, data_fingerprint: str) -> dict:
        """返回“版本×数据”绑定；数据指纹不符立即报错，不返回可混用的结果。"""
        version = self.get(version_id)
        if data_fingerprint != version.data_fingerprint:
            raise VersionDataMismatch(
                f"{version_id!r} 绑定数据 {version.data_fingerprint[:12]}…，"
                f"拒绝与 {data_fingerprint[:12]}… 组合；旧模型不得套用新数据")
        return {
            "version_id": version_id,
            "algorithm_version": version.algorithm_version,
            "params_digest": version.params_digest(),
            "data_fingerprint": version.data_fingerprint,
            "hard_constraint_fingerprint": version.hard_constraint_fingerprint,
        }

    def assert_result_consistent(self, result: Mapping[str, Any], *,
                                 data_fingerprint: str) -> None:
        """复核一份已产出的结果是否由当前版本在当前数据上生成。

        旧版本结果、或旧数据结果冒充当前，都会被拒绝。
        """
        active = self.active()
        got_version = result.get("model_version_id")
        got_data = result.get("data_fingerprint")
        if got_version != active.version_id:
            raise VersionDataMismatch(
                f"结果来自版本 {got_version!r}，当前版本为 {active.version_id!r}；"
                "旧模型结果不得冒充当前")
        if got_data != data_fingerprint or got_data != active.data_fingerprint:
            raise VersionDataMismatch(
                f"结果数据指纹 {got_data!r} 与当前数据 {data_fingerprint!r} 不符；"
                "旧数据结果不得冒充当前")

    # ---- 序列化 ----
    def to_manifest(self) -> dict:
        return {
            "schema": REGISTRY_SCHEMA,
            "hard_constraints": sorted(self._hard_constraints),
            "hard_constraint_fingerprint": self._hard_fp,
            "active_version_id": self._active,
            "versions": [v.snapshot() for v in self._versions.values()],
            "events": deepcopy(self._events),
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "ModelRegistry":
        """从清单恢复注册表；硬约束指纹不符则拒绝加载（防止回退夹带弱化）。"""
        if manifest.get("schema") != REGISTRY_SCHEMA:
            raise ModelRegistryError(f"清单 schema 不符：{manifest.get('schema')!r}")
        codes = frozenset(manifest.get("hard_constraints") or [])
        registry = cls(codes)
        declared = manifest.get("hard_constraint_fingerprint")
        if declared != registry._hard_fp:
            raise HardConstraintDrift(
                "清单声明的硬约束指纹与内容不一致，拒绝加载以防夹带弱化")
        for item in manifest.get("versions") or []:
            if item.get("hard_constraint_fingerprint") != registry._hard_fp:
                raise HardConstraintDrift(
                    f"版本 {item.get('version_id')!r} 硬约束指纹与注册表冻结值不一致，拒绝加载")
            version = ModelVersion(
                version_id=item["version_id"],
                algorithm_version=item["algorithm_version"],
                params=deepcopy(dict(item.get("params") or {})),
                data_fingerprint=item["data_fingerprint"],
                hard_constraint_fingerprint=registry._hard_fp,
                metrics=deepcopy(item.get("metrics")),
                status=item.get("status", _STATUS_CANDIDATE),
                note=item.get("note"))
            registry._versions[version.version_id] = version
        active = manifest.get("active_version_id")
        if active is not None:
            registry.get(active)  # 校验存在
            registry._active = active
        registry._events = deepcopy(list(manifest.get("events") or []))
        return registry
