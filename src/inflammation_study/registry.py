"""衍生分析登记服务。

在只追加事件存储之上实现业务规则：

- 角色分工：统计人员登记计划/运行/公开材料；数据管理员负责队列快照、
  数据资产、参与者撤回；科学审阅者出具相关性/因果/待验证机制结论；
  平台主管维护分析名额并可见全量谱系。
- 运行回执幂等：相同回执且输入/参数指纹一致时沿用原记录；
  指纹不一致不覆盖原记录，而是追加隔离事件。
- 名额按计划设池，授予/释放成对记账，并发下不会超额。
- 结果一经签发即冻结，后续只允许追加失效事件，不可改写。
- 参与者撤回或影像质控规则更正时，沿 运行→结论→公开材料
  依赖链标记失效；历史快照事件永不删除。
- 公开材料必须引用已签发、且当前仍有效的冻结结果版本。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .errors import AuthorizationError, ConflictError, NotFound, QuotaExhausted, StateError

# 角色
STATISTICIAN = "statistician"
DATA_STEWARD = "data_steward"
SCIENTIFIC_REVIEWER = "scientific_reviewer"
PLATFORM_ADMIN = "platform_admin"
PUBLIC = "public"

ROLES = {STATISTICIAN, DATA_STEWARD, SCIENTIFIC_REVIEWER, PLATFORM_ADMIN, PUBLIC}

_ASSET_KINDS = {
    "variable_dictionary",
    "exclusion_reason",
    "biomarker_batch",
    "imaging_qc",
    "genetic_instrument",
    "covariate_scheme",
}
_CLAIM_KINDS = {"correlation_association", "causal_inference", "mechanism_provisional"}
_REASON_KINDS = {"participant_withdrawal", "qc_rule_correction", "upstream_invalidation"}


@dataclass(frozen=True)
class Principal:
    """请求身份与角色。"""

    principal_id: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"未知角色: {self.role}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:12]}"


@dataclass
class _State:
    """从事件流重放出的聚合状态。"""

    events: list[dict[str, Any]]

    def __post_init__(self) -> None:
        self.by_aggregate: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for event in self.events:
            self.by_aggregate.setdefault(
                (event["aggregate_type"], event["aggregate_id"]), []
            ).append(event)
        # 回执 -> 首个正式运行记录
        self.runs_by_receipt: dict[str, dict[str, Any]] = {}
        for event in self.events:
            if event["event_type"] != "RUN_RECORDED":
                continue
            self.runs_by_receipt.setdefault(event["payload"]["receipt_id"], event)

    def history(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        return self.by_aggregate.get((aggregate_type, aggregate_id), [])

    def exists(self, aggregate_type: str, aggregate_id: str) -> bool:
        return bool(self.history(aggregate_type, aggregate_id))


class Registry:
    def __init__(self, store: Any) -> None:
        self.store = store
        self._lock = threading.RLock()

    # ================================================================ 辅助

    def _state(self) -> _State:
        return _State(self.store.events())

    def _require_role(self, actor: Principal, allowed: set[str], action: str) -> None:
        if actor.role not in allowed:
            raise AuthorizationError(
                f"角色 {actor.role} 无权执行 {action}", field="principal.role"
            )

    def _append(
        self,
        state: _State,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        version = len(state.history(aggregate_type, aggregate_id)) + 1
        event = {
            "event_id": event_id or _new_event_id(),
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at or _now(),
            "version": version,
            "payload": dict(payload),
        }
        stored = self.store.append(event)
        state.events.append(stored)
        state.by_aggregate.setdefault((aggregate_type, aggregate_id), []).append(stored)
        return stored

    @staticmethod
    def _invalidation(history: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in reversed(history):
            if event["event_type"] == "DEPENDENCY_INVALIDATED":
                return event
            if event["event_type"] in {"RUN_QUARANTINED"}:
                continue
        return None

    # ============================================================ 数据侧

    def freeze_cohort(
        self,
        actor: Principal,
        cohort_id: str,
        *,
        label: str = "",
        sample_refs: list[str] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """冻结队列快照。sample_refs 仅用于登记侧的影响面分析。"""
        self._require_role(actor, {DATA_STEWARD, PLATFORM_ADMIN}, "freeze_cohort")
        with self._lock:
            state = self._state()
            if state.exists("cohort_snapshot", cohort_id):
                raise ConflictError("队列快照已冻结，不可改写", field="cohort_id")
            return self._append(
                state,
                event_type="COHORT_FROZEN",
                aggregate_type="cohort_snapshot",
                aggregate_id=cohort_id,
                payload={
                    "label": label,
                    "frozen_by": actor.principal_id,
                    "sample_refs": sample_refs or [],
                },
                event_id=event_id,
            )

    def register_asset(
        self,
        actor: Principal,
        asset_id: str,
        asset_kind: str,
        fingerprint: str,
        cohort_id: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """登记变量字典、排除理由、标志物批次、影像质控、遗传工具或协变量方案。"""
        self._require_role(actor, {DATA_STEWARD, PLATFORM_ADMIN}, "register_asset")
        if asset_kind not in _ASSET_KINDS:
            raise StateError(f"未知资产类型: {asset_kind}", field="asset_kind")
        with self._lock:
            state = self._state()
            if not state.exists("cohort_snapshot", cohort_id):
                raise NotFound("引用的队列快照不存在", field="cohort_id")
            history = state.history("data_asset", asset_id)
            if history:
                if history[-1]["payload"]["asset_kind"] != asset_kind:
                    raise ConflictError("同一资产标识不可变更类型", field="asset_kind")
                if history[-1]["payload"]["fingerprint"] == fingerprint:
                    # 同一版本重复登记：幂等返回最近事件
                    return history[-1]
            return self._append(
                state,
                event_type="ASSET_REGISTERED",
                aggregate_type="data_asset",
                aggregate_id=asset_id,
                payload={
                    "asset_kind": asset_kind,
                    "fingerprint": fingerprint,
                    "cohort_id": cohort_id,
                    "registered_by": actor.principal_id,
                },
                event_id=event_id,
            )

    # ============================================================ 计划侧

    def register_plan(
        self,
        actor: Principal,
        plan_id: str,
        cohort_id: str,
        *,
        variable_versions: Mapping[str, str],
        exclusion_rules: list[str],
        asset_ids: list[str] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, {STATISTICIAN, PLATFORM_ADMIN}, "register_plan")
        with self._lock:
            state = self._state()
            if state.exists("analysis_plan", plan_id):
                raise ConflictError("分析计划已登记，标识不可复用", field="plan_id")
            if not state.exists("cohort_snapshot", cohort_id):
                raise NotFound("引用的队列快照不存在", field="cohort_id")
            for asset_id in asset_ids or []:
                if not state.exists("data_asset", asset_id):
                    raise NotFound(f"计划引用的资产不存在: {asset_id}", field="asset_ids")
            return self._append(
                state,
                event_type="PLAN_REGISTERED",
                aggregate_type="analysis_plan",
                aggregate_id=plan_id,
                payload={
                    "variable_versions": dict(variable_versions),
                    "exclusion_rules": list(exclusion_rules),
                    "cohort_id": cohort_id,
                    "asset_ids": list(asset_ids or []),
                    "registered_by": actor.principal_id,
                },
                event_id=event_id,
            )

    # ============================================================ 名额

    def create_slot_pool(
        self, actor: Principal, plan_id: str, capacity: int, *, event_id: str | None = None
    ) -> dict[str, Any]:
        """为计划创建有限分析名额池（每计划一个池）。"""
        self._require_role(actor, {PLATFORM_ADMIN}, "create_slot_pool")
        if capacity < 1:
            raise StateError("名额池容量必须为正整数", field="capacity")
        with self._lock:
            state = self._state()
            if not state.exists("analysis_plan", plan_id):
                raise NotFound("分析计划不存在", field="plan_id")
            if any(
                e["event_type"] == "SLOT_GRANTED" and e["payload"].get("pool_created")
                for e in state.history("analysis_plan", plan_id)
            ):
                raise ConflictError("该计划的名额池已存在", field="plan_id")
            return self._append(
                state,
                event_type="SLOT_GRANTED",
                aggregate_type="analysis_plan",
                aggregate_id=plan_id,
                payload={"allocation_id": f"pool:{plan_id}", "capacity": capacity, "pool_created": True},
                event_id=event_id,
            )

    def _pool_capacity(self, state: _State, plan_id: str) -> int:
        for event in state.history("analysis_plan", plan_id):
            if event["event_type"] == "SLOT_GRANTED" and event["payload"].get("pool_created"):
                return int(event["payload"]["capacity"])
        raise NotFound("该计划尚未创建名额池", field="plan_id")

    def grant_slot(
        self, actor: Principal, plan_id: str, allocation_id: str, *, event_id: str | None = None
    ) -> dict[str, Any]:
        self._require_role(actor, {STATISTICIAN, PLATFORM_ADMIN}, "grant_slot")
        with self._lock:
            state = self._state()
            history = state.history("analysis_plan", plan_id)
            granted = any(
                e["event_type"] == "SLOT_GRANTED" and e["payload"].get("allocation_id") == allocation_id
                for e in history
            )
            released = any(
                e["event_type"] == "SLOT_RELEASED" and e["payload"].get("allocation_id") == allocation_id
                for e in history
            )
            if granted:
                if released:
                    raise StateError("名额已被使用，不能重复授予", field="allocation_id")
                # 幂等：相同 allocation_id 的重复申请沿用原授予
                return next(
                    e
                    for e in history
                    if e["event_type"] == "SLOT_GRANTED"
                    and e["payload"].get("allocation_id") == allocation_id
                )
            capacity = self._pool_capacity(state, plan_id)
            used = {
                e["payload"]["allocation_id"]
                for e in state.history("analysis_plan", plan_id)
                if e["event_type"] == "SLOT_GRANTED" and not e["payload"].get("pool_created")
            }
            used -= {
                e["payload"]["allocation_id"]
                for e in state.history("analysis_plan", plan_id)
                if e["event_type"] == "SLOT_RELEASED"
            }
            if len(used) >= capacity:
                raise QuotaExhausted(f"计划 {plan_id} 的分析名额已用尽（容量 {capacity}）")
            return self._append(
                state,
                event_type="SLOT_GRANTED",
                aggregate_type="analysis_plan",
                aggregate_id=plan_id,
                payload={"allocation_id": allocation_id, "plan_id": plan_id},
                event_id=event_id,
            )

    def _release_slot(
        self, state: _State, plan_id: str, allocation_id: str
    ) -> dict[str, Any] | None:
        history = state.history("analysis_plan", plan_id)
        granted = any(
            e["event_type"] == "SLOT_GRANTED" and e["payload"].get("allocation_id") == allocation_id
            for e in history
        )
        released = any(
            e["event_type"] == "SLOT_RELEASED" and e["payload"].get("allocation_id") == allocation_id
            for e in history
        )
        if not granted or released:
            return None
        return self._append(
            state,
            event_type="SLOT_RELEASED",
            aggregate_type="analysis_plan",
            aggregate_id=plan_id,
            payload={"allocation_id": allocation_id, "plan_id": plan_id},
        )

    # ============================================================ 运行

    def record_run(
        self,
        actor: Principal,
        run_id: str,
        *,
        receipt_id: str,
        allocation_id: str,
        input_hash: str,
        parameter_hash: str,
        code_version: str,
        plan_id: str,
        cohort_id: str,
        asset_refs: list[Mapping[str, str]] | None = None,
        sample_refs: list[str] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """提交运行回执。

        相同回执 + 相同指纹：沿用原运行记录（幂等）。
        相同回执 + 不同指纹：原记录保留，本次写入隔离区。
        任何终态提交（成功记录或隔离）都消耗一个名额。
        """
        self._require_role(actor, {STATISTICIAN, PLATFORM_ADMIN}, "record_run")
        with self._lock:
            state = self._state()
            if not state.exists("analysis_plan", plan_id):
                raise NotFound("分析计划不存在", field="plan_id")
            if not state.exists("cohort_snapshot", cohort_id):
                raise NotFound("队列快照不存在", field="cohort_id")
            for ref in asset_refs or []:
                if not state.exists("data_asset", ref["asset_id"]):
                    raise NotFound(f"运行引用的资产不存在: {ref['asset_id']}", field="asset_refs")

            stored_run = state.runs_by_receipt.get(receipt_id)
            if stored_run is not None:
                same = (
                    stored_run["payload"]["input_hash"] == input_hash
                    and stored_run["payload"]["parameter_hash"] == parameter_hash
                )
                if same:
                    # 幂等：回执与指纹完全一致，沿用原记录
                    return stored_run
                # 同回执但不同指纹：相同的隔离提交重复出现时沿用原隔离记录
                existing_quarantine = next(
                    (
                        e
                        for e in state.events
                        if e["event_type"] == "RUN_QUARANTINED"
                        and e["payload"]["receipt_id"] == receipt_id
                        and e["payload"]["submitted_input_hash"] == input_hash
                        and e["payload"]["submitted_parameter_hash"] == parameter_hash
                    ),
                    None,
                )
                if existing_quarantine is not None:
                    return existing_quarantine
                # 先验证名额有效，再落隔离事件；任何终态提交都消耗名额
                self._require_allocation(state, plan_id, allocation_id)
                quarantine_id = self._quarantine_id(state, receipt_id)
                quarantined = self._append(
                    state,
                    event_type="RUN_QUARANTINED",
                    aggregate_type="run_record",
                    aggregate_id=quarantine_id,
                    payload={
                        "receipt_id": receipt_id,
                        "submitted_input_hash": input_hash,
                        "submitted_parameter_hash": parameter_hash,
                        "stored_event_id": stored_run["event_id"],
                        "stored_input_hash": stored_run["payload"]["input_hash"],
                        "stored_parameter_hash": stored_run["payload"]["parameter_hash"],
                        "reason": "fingerprint_mismatch",
                        "submitted_by": actor.principal_id,
                    },
                )
                self._release_slot(state, plan_id, allocation_id)
                return quarantined

            # 全新回执：校验通过后再消耗名额并落记录
            self._require_allocation(state, plan_id, allocation_id)
            recorded = self._append(
                state,
                event_type="RUN_RECORDED",
                aggregate_type="run_record",
                aggregate_id=run_id,
                payload={
                    "receipt_id": receipt_id,
                    "input_hash": input_hash,
                    "parameter_hash": parameter_hash,
                    "code_version": code_version,
                    "plan_id": plan_id,
                    "cohort_id": cohort_id,
                    "allocation_id": allocation_id,
                    "asset_refs": [dict(r) for r in (asset_refs or [])],
                    "sample_refs": list(sample_refs or []),
                    "submitted_by": actor.principal_id,
                },
                event_id=event_id,
            )
            self._release_slot(state, plan_id, allocation_id)
            return recorded

    def _require_allocation(self, state: _State, plan_id: str, allocation_id: str) -> None:
        grants = [
            e
            for e in state.history("analysis_plan", plan_id)
            if e["event_type"] == "SLOT_GRANTED" and e["payload"].get("allocation_id") == allocation_id
        ]
        releases = {
            e["payload"].get("allocation_id")
            for e in state.history("analysis_plan", plan_id)
            if e["event_type"] == "SLOT_RELEASED"
        }
        if not grants:
            raise StateError("运行必须携带已授予的名额", field="allocation_id")
        if allocation_id in releases:
            raise StateError("名额已被使用，不能重复提交", field="allocation_id")

    @staticmethod
    def _quarantine_id(state: _State, receipt_id: str) -> str:
        count = sum(
            1
            for e in state.events
            if e["event_type"] == "RUN_QUARANTINED"
            and e["payload"]["receipt_id"] == receipt_id
        )
        return f"quarantine:{receipt_id}:{count + 1}"

    def issue_result(
        self,
        actor: Principal,
        run_id: str,
        result_version: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """签发运行结果；签发后该运行冻结，不可再改写。"""
        self._require_role(actor, {STATISTICIAN, PLATFORM_ADMIN}, "issue_result")
        with self._lock:
            state = self._state()
            history = state.history("run_record", run_id)
            if not history or history[0]["event_type"] != "RUN_RECORDED":
                raise NotFound("运行记录不存在", field="run_id")
            issued = [e for e in history if e["event_type"] == "RESULT_ISSUED"]
            if issued:
                if issued[0]["payload"]["result_version"] == result_version:
                    return issued[0]
                raise StateError("结果已签发且冻结，不能改写或补发其他版本", field="result_version")
            if self._invalidation(history):
                raise StateError("运行已被标记失效，不能签发结果", field="run_id")
            return self._append(
                state,
                event_type="RESULT_ISSUED",
                aggregate_type="run_record",
                aggregate_id=run_id,
                payload={"result_version": result_version, "issued_by": actor.principal_id},
                event_id=event_id,
            )

    # ============================================================ 审阅

    def review_claim(
        self,
        actor: Principal,
        claim_id: str,
        *,
        run_id: str,
        claim_kind: str,
        claim_text: str,
        decision: str,
        frozen_result_version: str,
        comment: str = "",
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """科学审阅：区分相关性、因果推断与待验证机制，接受或退回。"""
        self._require_role(actor, {SCIENTIFIC_REVIEWER, PLATFORM_ADMIN}, "review_claim")
        if claim_kind not in _CLAIM_KINDS:
            raise StateError("未知结论类型", field="claim_kind")
        if decision not in {"accepted", "returned"}:
            raise StateError("审阅结论必须是 accepted 或 returned", field="decision")
        with self._lock:
            state = self._state()
            run_history = state.history("run_record", run_id)
            issued = next(
                (e for e in run_history if e["event_type"] == "RESULT_ISSUED"), None
            )
            if issued is None:
                raise NotFound("运行结果尚未签发，不能审阅", field="run_id")
            if issued["payload"]["result_version"] != frozen_result_version:
                raise ConflictError("审阅必须引用运行当前冻结的结果版本", field="frozen_result_version")
            if self._invalidation(run_history):
                raise StateError("运行已失效，其冻结版本不能支撑新结论", field="run_id")
            if state.exists("research_claim", claim_id):
                raise ConflictError("结论标识已存在", field="claim_id")
            return self._append(
                state,
                event_type="CLAIM_REVIEWED",
                aggregate_type="research_claim",
                aggregate_id=claim_id,
                payload={
                    "claim_kind": claim_kind,
                    "claim_text": claim_text,
                    "decision": decision,
                    "reviewer_id": actor.principal_id,
                    "run_id": run_id,
                    "frozen_result_version": frozen_result_version,
                    "comment": comment,
                },
                event_id=event_id,
            )

    # ============================================================ 公开

    def publish_material(
        self,
        actor: Principal,
        material_id: str,
        *,
        claim_id: str,
        title: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """公开表述必须引用已接受且未失效结论所冻结的分析版本。"""
        self._require_role(actor, {STATISTICIAN, PLATFORM_ADMIN}, "publish_material")
        with self._lock:
            state = self._state()
            claim_history = state.history("research_claim", claim_id)
            review = next((e for e in claim_history if e["event_type"] == "CLAIM_REVIEWED"), None)
            if review is None:
                raise NotFound("结论不存在", field="claim_id")
            if review["payload"]["decision"] != "accepted":
                raise StateError("只有审阅接受的结论可以公开引用", field="claim_id")
            if self._invalidation(claim_history):
                raise StateError("结论已标记失效，不能用于新的公开表述", field="claim_id")
            if state.exists("public_material", material_id):
                raise ConflictError("公开材料标识已存在", field="material_id")
            return self._append(
                state,
                event_type="MATERIAL_PUBLISHED",
                aggregate_type="public_material",
                aggregate_id=material_id,
                payload={
                    "claim_id": claim_id,
                    "run_id": review["payload"]["run_id"],
                    "frozen_result_version": review["payload"]["frozen_result_version"],
                    "title": title,
                    "published_by": actor.principal_id,
                },
                event_id=event_id,
            )

    # ============================================================ 撤回/更正

    def record_withdrawal(
        self,
        actor: Principal,
        withdrawal_id: str,
        *,
        participant_ref: str,
        cohort_id: str,
        event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """登记参与者撤回，并沿依赖链标记所有受影响结果需重算。"""
        self._require_role(actor, {DATA_STEWARD, PLATFORM_ADMIN}, "record_withdrawal")
        with self._lock:
            state = self._state()
            if not state.exists("cohort_snapshot", cohort_id):
                raise NotFound("队列快照不存在", field="cohort_id")
            # 同一参与者在同一队列重复撤回：幂等返回既有事件，不重复传播
            for event in state.events:
                if (
                    event["event_type"] == "WITHDRAWAL_RECEIVED"
                    and event["payload"]["participant_ref"] == participant_ref
                    and event["payload"]["cohort_id"] == cohort_id
                ):
                    return [event]
            withdrawal = self._append(
                state,
                event_type="WITHDRAWAL_RECEIVED",
                aggregate_type="participant_withdrawal",
                aggregate_id=withdrawal_id,
                payload={
                    "participant_ref": participant_ref,
                    "cohort_id": cohort_id,
                    "recorded_by": actor.principal_id,
                },
                event_id=event_id,
            )
            affected_runs = [
                e
                for e in state.events
                if e["event_type"] == "RUN_RECORDED"
                and e["payload"].get("cohort_id") == cohort_id
                and participant_ref in e["payload"].get("sample_refs", [])
            ]
            cascaded: list[dict[str, Any]] = [withdrawal]
            for run in affected_runs:
                cascaded.extend(
                    self._invalidate_chain(
                        state,
                        run["aggregate_id"],
                        reason_kind="participant_withdrawal",
                        source_event_id=withdrawal["event_id"],
                    )
                )
            return cascaded

    def correct_asset(
        self,
        actor: Principal,
        asset_id: str,
        new_fingerprint: str,
        *,
        cohort_id: str,
        event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """影像质控等规则更正：登记新版本资产，旧指纹产物全部标记重算。"""
        self._require_role(actor, {DATA_STEWARD, PLATFORM_ADMIN}, "correct_asset")
        with self._lock:
            state = self._state()
            history = state.history("data_asset", asset_id)
            if not history:
                raise NotFound("资产不存在，应先登记而非更正", field="asset_id")
            old_fingerprint = history[-1]["payload"]["fingerprint"]
            asset_kind = history[-1]["payload"]["asset_kind"]
            if old_fingerprint == new_fingerprint:
                return [history[-1]]
            new_asset = self.register_asset(
                actor,
                asset_id,
                asset_kind,
                new_fingerprint,
                cohort_id,
                event_id=event_id,
            )
            affected_runs = [
                e
                for e in state.events
                if e["event_type"] == "RUN_RECORDED"
                and any(
                    r.get("asset_id") == asset_id and r.get("fingerprint") == old_fingerprint
                    for r in e["payload"].get("asset_refs", [])
                )
            ]
            cascaded = [new_asset]
            for run in affected_runs:
                cascaded.extend(
                    self._invalidate_chain(
                        state,
                        run["aggregate_id"],
                        reason_kind="qc_rule_correction",
                        source_event_id=new_asset["event_id"],
                    )
                )
            return cascaded

    def _invalidate_chain(
        self, state: _State, run_id: str, *, reason_kind: str, source_event_id: str
    ) -> list[dict[str, Any]]:
        """沿 run → claim → material 传播失效标记。"""
        if reason_kind not in _REASON_KINDS:
            raise StateError("未知失效原因", field="reason_kind")
        emitted: list[dict[str, Any]] = []

        run_history = state.history("run_record", run_id)
        if not run_history or run_history[0]["event_type"] != "RUN_RECORDED":
            return emitted
        run_event = self._invalidate_one(
            state, "run_record", run_id, reason_kind, source_event_id
        )
        if run_event is None:
            return emitted  # 已失效，传播此前已完成
        emitted.append(run_event)

        for claim_history in state.by_aggregate.values():
            review = next(
                (e for e in claim_history if e["event_type"] == "CLAIM_REVIEWED"), None
            )
            if review is None or review["payload"]["run_id"] != run_id:
                continue
            claim_id = review["aggregate_id"]
            claim_event = self._invalidate_one(
                state, "research_claim", claim_id, "upstream_invalidation", run_event["event_id"]
            )
            if claim_event is not None:
                emitted.append(claim_event)
            for material_history in state.by_aggregate.values():
                published = next(
                    (e for e in material_history if e["event_type"] == "MATERIAL_PUBLISHED"), None
                )
                if published is None or published["payload"]["claim_id"] != claim_id:
                    continue
                material_event = self._invalidate_one(
                    state,
                    "public_material",
                    published["aggregate_id"],
                    "upstream_invalidation",
                    (claim_event or run_event)["event_id"],
                )
                if material_event is not None:
                    emitted.append(material_event)
        return emitted

    def _invalidate_one(
        self,
        state: _State,
        aggregate_type: str,
        aggregate_id: str,
        reason_kind: str,
        source_event_id: str,
    ) -> dict[str, Any] | None:
        history = state.history(aggregate_type, aggregate_id)
        if self._invalidation(history):
            return None
        return self._append(
            state,
            event_type="DEPENDENCY_INVALIDATED",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload={
                "reason_kind": reason_kind,
                "source_event_id": source_event_id,
            },
        )

    # ============================================================ 谱系

    def find_claims_by_text(self, query: str) -> list[str]:
        """按风险表述文本检索结论标识（大小写不敏感子串匹配）。"""
        query_lower = query.lower()
        return [
            e["aggregate_id"]
            for e in self.store.events()
            if e["event_type"] == "CLAIM_REVIEWED"
            and query_lower in e["payload"].get("claim_text", "").lower()
        ]

    def trace(self, target_id: str, actor: Principal) -> dict[str, Any]:
        """从公开材料、结论或运行出发，给出按角色裁剪的最小数据谱系。"""
        with self._lock:
            state = self._state()
            view = self._build_trace(state, target_id, actor)
        return _redact(view, actor.role)

    def _build_trace(self, state: _State, target_id: str, actor: Principal) -> dict[str, Any]:
        material_history = state.history("public_material", target_id)
        claim_history = state.history("research_claim", target_id)
        run_history = state.history("run_record", target_id)

        material_event = next(
            (e for e in material_history if e["event_type"] == "MATERIAL_PUBLISHED"), None
        )
        claim_event = next(
            (e for e in claim_history if e["event_type"] == "CLAIM_REVIEWED"), None
        )
        run_event = run_history[0] if run_history and run_history[0]["event_type"] == "RUN_RECORDED" else None

        if actor.role == PUBLIC:
            if material_event is None:
                raise AuthorizationError("匿名视角只能查看已公开材料的谱系", field="target_id")
        elif not (material_event or claim_event or run_event):
            raise NotFound("未找到对应的公开材料、结论或运行", field="target_id")

        if material_event is not None:
            claim_event = claim_event or next(
                (
                    e
                    for e in state.events
                    if e["event_type"] == "CLAIM_REVIEWED"
                    and e["aggregate_id"] == material_event["payload"]["claim_id"]
                ),
                None,
            )
        if claim_event is not None:
            run_id = claim_event["payload"]["run_id"]
            run_history = state.history("run_record", run_id)
            run_event = run_event or next(
                (e for e in run_history if e["event_type"] == "RUN_RECORDED"), None
            )

        view: dict[str, Any] = {
            "target": {"kind": "unknown", "id": target_id},
        }
        if material_event is not None:
            view["target"] = {"kind": "public_material", "id": target_id}
            view["status"] = "invalidated" if self._invalidation(material_history) else "published"
            view["material"] = {
                "id": target_id,
                "title": material_event["payload"]["title"],
                "claim_id": material_event["payload"]["claim_id"],
                "frozen_result_version": material_event["payload"]["frozen_result_version"],
                "event_id": material_event["event_id"],
            }
        elif claim_event is not None:
            view["target"] = {"kind": "research_claim", "id": target_id}
        elif run_event is not None:
            view["target"] = {"kind": "run_record", "id": target_id}

        if claim_event is not None:
            ch = state.history("research_claim", claim_event["aggregate_id"])
            view.setdefault("status", "invalidated" if self._invalidation(ch) else claim_event["payload"]["decision"])
            view["claim"] = {
                "id": claim_event["aggregate_id"],
                "claim_kind": claim_event["payload"]["claim_kind"],
                "claim_text": claim_event["payload"]["claim_text"],
                "decision": claim_event["payload"]["decision"],
                "reviewer_id": claim_event["payload"]["reviewer_id"],
                "comment": claim_event["payload"].get("comment", ""),
                "frozen_result_version": claim_event["payload"]["frozen_result_version"],
                "run_id": claim_event["payload"]["run_id"],
                "event_id": claim_event["event_id"],
                "invalidation": _invalidation_payload(self._invalidation(ch)),
            }

        if run_event is not None:
            rid = run_event["aggregate_id"]
            rh = state.history("run_record", rid)
            issued = next((e for e in rh if e["event_type"] == "RESULT_ISSUED"), None)
            view.setdefault(
                "status", "invalidated" if self._invalidation(rh) else ("issued" if issued else "recorded")
            )
            view["run"] = {
                "id": rid,
                "receipt_id": run_event["payload"]["receipt_id"],
                "code_version": run_event["payload"]["code_version"],
                "input_hash": run_event["payload"]["input_hash"],
                "parameter_hash": run_event["payload"]["parameter_hash"],
                "allocation_id": run_event["payload"]["allocation_id"],
                "plan_id": run_event["payload"]["plan_id"],
                "cohort_id": run_event["payload"]["cohort_id"],
                "asset_refs": run_event["payload"].get("asset_refs", []),
                "sample_refs": run_event["payload"].get("sample_refs", []),
                "event_id": run_event["event_id"],
                "issued": (
                    {
                        "result_version": issued["payload"]["result_version"],
                        "issued_by": issued["payload"]["issued_by"],
                        "event_id": issued["event_id"],
                    }
                    if issued
                    else None
                ),
                "invalidation": _invalidation_payload(self._invalidation(rh)),
            }

            plan_id = run_event["payload"]["plan_id"]
            plan_history = state.history("analysis_plan", plan_id)
            plan_event = next((e for e in plan_history if e["event_type"] == "PLAN_REGISTERED"), None)
            if plan_event is not None:
                ph = plan_event["payload"]
                view["plan"] = {
                    "id": plan_id,
                    "variable_versions": ph["variable_versions"],
                    "exclusion_rules": ph["exclusion_rules"],
                    "cohort_id": ph.get("cohort_id"),
                    "asset_ids": ph.get("asset_ids", []),
                    "event_id": plan_event["event_id"],
                }

            cohort_id = run_event["payload"]["cohort_id"]
            cohort_history = state.history("cohort_snapshot", cohort_id)
            cohort_event = next((e for e in cohort_history if e["event_type"] == "COHORT_FROZEN"), None)
            if cohort_event is not None:
                view["cohort"] = {
                    "id": cohort_id,
                    "label": cohort_event["payload"].get("label", ""),
                    "sample_count": len(cohort_event["payload"].get("sample_refs", [])),
                    "event_id": cohort_event["event_id"],
                    "invalidation": _invalidation_payload(self._invalidation(cohort_history)),
                }

            assets = []
            current_fingerprints = {
                aid: history[-1]["payload"]["fingerprint"]
                for (kind, aid), history in state.by_aggregate.items()
                if kind == "data_asset"
            }
            for ref in run_event["payload"].get("asset_refs", []):
                assets.append(
                    {
                        "asset_id": ref["asset_id"],
                        "used_fingerprint": ref["fingerprint"],
                        "current_fingerprint": current_fingerprints.get(ref["asset_id"]),
                        "stale": current_fingerprints.get(ref["asset_id"]) != ref["fingerprint"],
                    }
                )
            if assets:
                view["assets"] = assets

            withdrawals = [
                {
                    "withdrawal_id": e["aggregate_id"],
                    "participant_ref": e["payload"]["participant_ref"],
                    "cohort_id": e["payload"]["cohort_id"],
                    "event_id": e["event_id"],
                }
                for e in state.events
                if e["event_type"] == "WITHDRAWAL_RECEIVED"
                and e["payload"]["cohort_id"] == cohort_id
                and e["payload"]["participant_ref"] in run_event["payload"].get("sample_refs", [])
            ]
            if withdrawals:
                view["withdrawals"] = withdrawals

            downstream_claims = []
            downstream_materials = []
            for e in state.events:
                if e["event_type"] != "CLAIM_REVIEWED" or e["payload"]["run_id"] != rid:
                    continue
                cid = e["aggregate_id"]
                ch = state.history("research_claim", cid)
                invalidation = self._invalidation(ch)
                downstream_claims.append(
                    {
                        "claim_id": cid,
                        "claim_kind": e["payload"]["claim_kind"],
                        "claim_text": e["payload"]["claim_text"],
                        "frozen_result_version": e["payload"]["frozen_result_version"],
                        "decision": e["payload"]["decision"],
                        "reviewer_id": e["payload"]["reviewer_id"],
                        "comment": e["payload"].get("comment", ""),
                        "status": "invalidated" if invalidation else e["payload"]["decision"],
                    }
                )
                for m in state.events:
                    if m["event_type"] != "MATERIAL_PUBLISHED" or m["payload"]["claim_id"] != cid:
                        continue
                    mh = state.history("public_material", m["aggregate_id"])
                    downstream_materials.append(
                        {
                            "material_id": m["aggregate_id"],
                            "title": m["payload"]["title"],
                            "claim_id": cid,
                            "frozen_result_version": m["payload"]["frozen_result_version"],
                            "status": "invalidated" if self._invalidation(mh) else "published",
                        }
                    )
            if downstream_claims:
                view["downstream_claims"] = downstream_claims
            if downstream_materials:
                view["downstream_materials"] = downstream_materials

        if actor.role == PLATFORM_ADMIN:
            kinds = []
            if material_event:
                kinds += [("public_material", target_id)]
            if claim_event:
                kinds += [("research_claim", claim_event["aggregate_id"])]
            if run_event:
                rid = run_event["aggregate_id"]
                kinds += [("run_record", rid)]
                run_payload = run_event["payload"]
                kinds += [
                    ("analysis_plan", run_payload["plan_id"]),
                    ("cohort_snapshot", run_payload["cohort_id"]),
                ]
            view["event_log"] = [
                {
                    "event_id": e["event_id"],
                    "event_type": e["event_type"],
                    "aggregate_type": e["aggregate_type"],
                    "aggregate_id": e["aggregate_id"],
                    "version": e["version"],
                    "occurred_at": e["occurred_at"],
                }
                for kind, agg_id in dict.fromkeys(kinds)
                for e in state.history(kind, agg_id)
            ]
        return view


def _invalidation_payload(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "reason_kind": event["payload"]["reason_kind"],
        "source_event_id": event["payload"]["source_event_id"],
        "event_id": event["event_id"],
    }


# 字段按敏感原因分组，按角色做最小可见裁剪。
#   review    审阅意见与审阅决定（审阅者、统计人员、主管）
#   technical 事件标识、指纹、名额、签发人、计划技术细节（统计人员、主管）
#   steward   样本与参与者信息（数据管理员、主管）
# 未登记字段属于基线信息（编号、冻结版本、代码版本、失效状态），对所有角色可见，
# 公开材料的匿名视角也能看到它所引用的冻结分析版本。
_FIELD_GROUPS: dict[tuple[str, ...], str] = {
    ("material", "event_id"): "technical",
    ("claim", "reviewer_id"): "review",
    ("claim", "comment"): "review",
    ("claim", "decision"): "review",
    ("claim", "event_id"): "technical",
    ("claim", "invalidation", "source_event_id"): "technical",
    ("claim", "invalidation", "event_id"): "technical",
    ("run", "receipt_id"): "technical",
    ("run", "input_hash"): "technical",
    ("run", "parameter_hash"): "technical",
    ("run", "allocation_id"): "technical",
    ("run", "asset_refs"): "technical",
    ("run", "sample_refs"): "steward",
    ("run", "event_id"): "technical",
    ("run", "issued"): "technical",
    ("run", "invalidation", "source_event_id"): "technical",
    ("run", "invalidation", "event_id"): "technical",
    ("plan", "variable_versions"): "technical",
    ("plan", "exclusion_rules"): "technical",
    ("plan", "asset_ids"): "technical",
    ("plan", "event_id"): "technical",
    ("cohort", "event_id"): "technical",
    ("assets", "used_fingerprint"): "technical",
    ("assets", "current_fingerprint"): "technical",
    ("downstream_claims", "claim_text"): "review",
    ("downstream_claims", "decision"): "review",
    ("downstream_claims", "reviewer_id"): "review",
    ("downstream_claims", "comment"): "review",
    ("downstream_materials", "title"): "review",
}

_SECTION_GROUPS = {
    "withdrawals": "steward",
    "event_log": "technical",
}

_GROUP_VISIBLE = {
    PLATFORM_ADMIN: {"review", "technical", "steward"},
    STATISTICIAN: {"review", "technical"},
    SCIENTIFIC_REVIEWER: {"review"},
    DATA_STEWARD: {"steward"},
    PUBLIC: set(),
}


def _redact(view: dict[str, Any], role: str) -> dict[str, Any]:
    """按角色递归裁剪谱系视图，实现最小数据谱系。"""
    allowed = _GROUP_VISIBLE[role]
    for section, group in _SECTION_GROUPS.items():
        if section in view and group not in allowed:
            del view[section]
    _redact_obj(view, (), allowed)
    return view


def _redact_obj(obj: Any, path: tuple[str, ...], allowed: set[str]) -> None:
    if isinstance(obj, dict):
        for key in list(obj):
            child_path = path + (key,)
            group = _FIELD_GROUPS.get(child_path)
            if group is not None and group not in allowed:
                del obj[key]
            else:
                _redact_obj(obj[key], child_path, allowed)
    elif isinstance(obj, list):
        # 列表元素沿用同一路径（assets、withdrawals、event_log 等）
        for item in obj:
            _redact_obj(item, path, allowed)
