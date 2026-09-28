"""衍生分析登记服务。

在追加写事件日志之上折叠出当前状态，并承载业务规则：

- 角色授权：统计人员登记计划/运行，数据管理员只管访问与撤回，
  科学审阅者区分相关性、因果推断与待验证机制，平台主管冻结队列与名额。
- 计划锁定与签发锁定：计划登记后不可改写，已签发结论只能通过
  新结论修订。
- 运行回执幂等：相同回执且输入/参数指纹一致沿用原记录；指纹不一致隔离。
- 名额并发：预留与释放串行校验，容量绝不超额。
- 失效级联：参与者撤回、样本排除、影像/标志物等质控规则更正时，
  沿 队列→计划→运行→结论→公开材料 的依赖标记重算，历史事件不删除。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .events import ContractViolation, EventStore

ROLE_ADMIN = "platform_admin"
ROLE_STATISTICIAN = "statistician"
ROLE_STEWARD = "data_steward"
ROLE_REVIEWER = "scientific_reviewer"

CLAIM_KINDS = ("association", "causal", "mechanism_provisional")


class RegistryError(Exception):
    """业务规则拒绝，消息可直接展示给调用方。"""


class PermissionDenied(RegistryError):
    """调用角色或数据访问权限不足。"""


class QuotaExhausted(RegistryError):
    """名额池余量不足以完成预留。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _eid(event_type: str, aggregate_id: str) -> str:
    return f"{event_type.lower()}-{aggregate_id}-{uuid.uuid4().hex[:10]}"


class Registry:
    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()
        self._lock = threading.RLock()
        self._state = self._fold(self.store.events)

    # ------------------------------------------------------------------ 状态折叠

    @staticmethod
    def _fold(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        state: dict[str, Any] = {
            "cohorts": {},          # cohort_id -> {definition_hash, frozen_at}
            "assets": {},           # asset_id -> {kind, version, fingerprint, participant_ids, status}
            "access": {},           # (subject_id, asset_id) -> granted/revoked
            "withdrawn": set(),
            "exclusions": {},       # participant_id -> [event]
            "exclusion_keys": set(),  # (participant_id, reason_code, rule_version)
            "quarantine_keys": set(),  # (receipt, input_hash, parameter_hash, plan_id)
            "plans": {},            # plan_id -> {...}
            "pools": {},            # pool_id -> {capacity, held: {holder: count}}
            "runs": {},             # run_id -> {...}
            "receipts": {},         # run_receipt -> run_id
            "claims": {},           # claim_id -> {...}
            "materials": {},        # material_id -> {...}
            "invalidated": set(),   # (aggregate_type, aggregate_id, source_event_id)
        }
        for event in events:
            Registry._apply(state, event)
        return state

    @staticmethod
    def _apply(state: dict[str, Any], event: Mapping[str, Any]) -> None:
        et = event["event_type"]
        aid = event["aggregate_id"]
        body = event.get("payload", {})
        if et == "COHORT_FROZEN":
            state["cohorts"][aid] = {
                "definition_hash": body["definition_hash"],
                "frozen_at": event["occurred_at"],
            }
        elif et == "DATA_ASSET_VERSIONED":
            asset = state["assets"].get(aid)
            members = set(asset.get("participant_ids", ())) if asset else set()
            members.update(body.get("participant_ids", ()))
            state["assets"][aid] = {
                "kind": body["asset_kind"],
                "version": body["asset_version"],
                "fingerprint": body["fingerprint"],
                "participant_ids": members,
                "status": "current",
            }
            if asset and body["asset_version"] > asset["version"]:
                pass  # 旧版本仍可在事件历史中读取，当前指针前移
        elif et == "ACCESS_GRANTED":
            state["access"][(body["subject_id"], body["asset_id"])] = "granted"
        elif et == "ACCESS_REVOKED":
            state["access"][(body["subject_id"], body["asset_id"])] = "revoked"
        elif et == "WITHDRAWAL_RECEIVED":
            state["withdrawn"].add(body["participant_id"])
        elif et == "PARTICIPANT_EXCLUDED":
            key = (body["participant_id"], body["reason_code"], body["rule_version"])
            state["exclusion_keys"].add(key)
            state["exclusions"].setdefault(body["participant_id"], []).append({
                "reason_code": body["reason_code"],
                "rule_version": body["rule_version"],
                "occurred_at": event["occurred_at"],
            })
        elif et == "PLAN_REGISTERED":
            state["plans"][aid] = {
                "cohort_id": body["cohort_id"],
                "code_version": body["code_version"],
                "variable_versions": dict(body["variable_versions"]),
                "exclusion_rules": list(body["exclusion_rules"]),
                "covariate_scheme_id": body.get("covariate_scheme_id"),
                "registered_by": body.get("registered_by"),
                "registered_at": event["occurred_at"],
                "runs": [],
            }
        elif et == "QUOTA_POOL_OPENED":
            state["pools"][aid] = {"capacity": body["capacity"], "held": {}}
        elif et == "SLOT_RESERVED":
            pool = state["pools"][body["quota_pool_id"]]
            holder = body["holder_id"]
            pool["held"][holder] = pool["held"].get(holder, 0) + body["slot_count"]
        elif et == "SLOT_RELEASED":
            pool = state["pools"][body["quota_pool_id"]]
            holder = body["holder_id"]
            pool["held"][holder] = max(0, pool["held"].get(holder, 0) - body["slot_count"])
        elif et == "RUN_RECORDED":
            run = {
                "run_id": aid,
                "plan_id": body["plan_id"],
                "run_receipt": body["run_receipt"],
                "input_hash": body["input_hash"],
                "parameter_hash": body["parameter_hash"],
                "recorded_by": body.get("recorded_by"),
                "recorded_at": event["occurred_at"],
                "status": "recorded",
                "invalidations": [],
            }
            state["runs"][aid] = run
            state["receipts"][body["run_receipt"]] = aid
            state["plans"][body["plan_id"]]["runs"].append(aid)
        elif et == "RUN_QUARANTINED":
            state["quarantine_keys"].add((
                body["run_receipt"], body.get("submitted_input_hash"),
                body.get("submitted_parameter_hash"), body.get("plan_id")))
            # 隔离的只是指纹不一致的重复提交；不得改动回执对应的原运行状态。
            state["runs"].setdefault(aid, {
                "run_id": aid,
                "plan_id": body.get("plan_id"),
                "run_receipt": body["run_receipt"],
                "status": "quarantined",
                "invalidations": [],
            })
        elif et == "CLAIM_REVIEWED":
            claim = state["claims"].setdefault(aid, {
                "claim_id": aid,
                "run_id": body.get("run_id"),
                "kind": body["claim_kind"],
                "reviews": [],
                "status": "reviewed",
                "invalidations": [],
            })
            claim["run_id"] = body.get("run_id", claim.get("run_id"))
            claim["kind"] = body["claim_kind"]
            claim["reviews"].append({
                "reviewer_id": body["reviewer_id"],
                "decision": body.get("decision"),
                "comments": body.get("comments"),
                "occurred_at": event["occurred_at"],
            })
        elif et == "CLAIM_ISSUED":
            claim = state["claims"][aid]
            claim["status"] = "issued"
            claim["statement_hash"] = body["statement_hash"]
            claim["frozen_references"] = list(body.get("frozen_references", ()))
            claim["issued_at"] = event["occurred_at"]
        elif et == "MATERIAL_PUBLISHED":
            state["materials"][aid] = {
                "material_id": aid,
                "kind": body["material_kind"],
                "title": body.get("title"),
                "frozen_references": list(body["frozen_references"]),
                "published_at": event["occurred_at"],
                "status": "published",
                "invalidations": [],
            }
        elif et == "DEPENDENCY_INVALIDATED":
            key = (event["aggregate_type"], aid, body["source_event_id"])
            if key in state["invalidated"]:
                return
            state["invalidated"].add(key)
            note = {"reason_code": body["reason_code"],
                    "source_event_id": body["source_event_id"],
                    "occurred_at": event["occurred_at"]}
            bucket = {
                "run_record": "runs",
                "research_claim": "claims",
                "public_material": "materials",
            }.get(event["aggregate_type"])
            if bucket and aid in state[bucket]:
                state[bucket][aid]["status"] = "invalidated"
                state[bucket][aid].setdefault("invalidations", []).append(note)

    def _commit(self, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """串行完成 校验角色之外的全部事件追加，并把新事件折叠进状态。"""
        with self._lock:
            appended = self.store.append_many(events)
            for event in appended:
                self._apply(self._state, event)
            return appended

    @staticmethod
    def _event(event_type: str, aggregate_type: str, aggregate_id: str,
               payload: Mapping[str, Any], occurred_at: str | None = None) -> dict[str, Any]:
        return {
            "event_id": _eid(event_type, aggregate_id),
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at or _now(),
            "payload": dict(payload),
        }

    # ------------------------------------------------------------------ 授权辅助

    @staticmethod
    def _require_role(actor: Mapping[str, str], *roles: str) -> None:
        if actor.get("role") not in roles:
            raise PermissionDenied(
                f"角色 {actor.get('role')} 无权执行此操作，需要: {', '.join(roles)}")

    # ------------------------------------------------------------------ 队列与资产

    def freeze_cohort(self, actor: Mapping[str, str], cohort_id: str, definition_hash: str,
                      participant_ids: Sequence[str], occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_ADMIN)
        if cohort_id in self._state["cohorts"]:
            raise RegistryError(f"队列快照 {cohort_id} 已冻结，快照不可改写")
        roster = self._event(
            "DATA_ASSET_VERSIONED", "data_asset", cohort_id,
            {"asset_kind": "cohort_roster", "asset_version": 1,
             "fingerprint": definition_hash, "participant_ids": list(participant_ids)},
            occurred_at)
        frozen = self._event(
            "COHORT_FROZEN", "cohort_snapshot", cohort_id,
            {"definition_hash": definition_hash}, occurred_at)
        appended = self._commit([roster, frozen])
        return appended[-1]

    def version_asset(self, actor: Mapping[str, str], asset_id: str, asset_kind: str,
                      fingerprint: str, occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_STEWARD, ROLE_ADMIN)
        current = self._state["assets"].get(asset_id)
        version = (current["version"] + 1) if current else 1
        event = self._event(
            "DATA_ASSET_VERSIONED", "data_asset", asset_id,
            {"asset_kind": asset_kind, "asset_version": version, "fingerprint": fingerprint},
            occurred_at)
        return self._commit([event])[0]

    def correct_qc_rule(self, actor: Mapping[str, str], asset_id: str, new_fingerprint: str,
                        occurred_at: str | None = None) -> list[dict[str, Any]]:
        """质控规则更正：资产出新版，并沿依赖级联失效。"""
        self._require_role(actor, ROLE_STEWARD, ROLE_ADMIN)
        asset = self._state["assets"].get(asset_id)
        if asset is None:
            raise RegistryError(f"资产 {asset_id} 尚未登记版本，无法更正")
        new_version = asset["version"] + 1
        versioned = self._event(
            "DATA_ASSET_VERSIONED", "data_asset", asset_id,
            {"asset_kind": asset["kind"], "asset_version": new_version,
             "fingerprint": new_fingerprint}, occurred_at)
        corrected = self._event(
            "QC_RULE_CORRECTED", "data_asset", asset_id,
            {"asset_id": asset_id, "old_version": asset["version"],
             "new_version": new_version}, occurred_at)
        appended = self._commit([versioned, corrected])
        cascade = self._cascade(corrected["event_id"], "qc_rule_corrected",
                                plan_ids=self._plans_referencing_asset(asset_id))
        return appended + cascade

    # ------------------------------------------------------------------ 访问、撤回、排除

    def grant_access(self, actor: Mapping[str, str], subject_id: str, asset_id: str,
                     occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_STEWARD, ROLE_ADMIN)
        if asset_id not in self._state["assets"]:
            raise RegistryError(f"资产 {asset_id} 不存在，无法授权")
        if self._state["access"].get((subject_id, asset_id)) == "granted":
            return self._existing_access_event(subject_id, asset_id, "ACCESS_GRANTED")
        event = self._event(
            "ACCESS_GRANTED", "access_grant", f"{subject_id}-{asset_id}",
            {"subject_id": subject_id, "asset_id": asset_id}, occurred_at)
        return self._commit([event])[0]

    def revoke_access(self, actor: Mapping[str, str], subject_id: str, asset_id: str,
                      occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_STEWARD, ROLE_ADMIN)
        if self._state["access"].get((subject_id, asset_id)) == "revoked":
            return self._existing_access_event(subject_id, asset_id, "ACCESS_REVOKED")
        event = self._event(
            "ACCESS_REVOKED", "access_grant", f"{subject_id}-{asset_id}",
            {"subject_id": subject_id, "asset_id": asset_id}, occurred_at)
        return self._commit([event])[0]

    def _existing_access_event(self, subject_id: str, asset_id: str, event_type: str) -> dict[str, Any]:
        for event in reversed(self.store.events):
            body = event.get("payload", {})
            if (event["event_type"] == event_type
                    and body.get("subject_id") == subject_id
                    and body.get("asset_id") == asset_id):
                return event
        raise RegistryError("授权已存在但找不到原始事件")

    def receive_withdrawal(self, actor: Mapping[str, str], participant_id: str,
                           occurred_at: str | None = None) -> list[dict[str, Any]]:
        self._require_role(actor, ROLE_STEWARD, ROLE_ADMIN)
        if participant_id in self._state["withdrawn"]:
            return []  # 业务幂等：同一参与者重复撤回沿用原状态
        withdrawal = self._event(
            "WITHDRAWAL_RECEIVED", "participant", participant_id,
            {"participant_id": participant_id}, occurred_at)
        appended = self._commit([withdrawal])
        if not appended:
            return []
        plan_ids = self._plans_containing_participant(participant_id)
        cascade = self._cascade(withdrawal["event_id"], "participant_withdrawn", plan_ids)
        return appended + cascade

    def exclude_participant(self, actor: Mapping[str, str], participant_id: str,
                            reason_code: str, rule_version: str,
                            occurred_at: str | None = None) -> list[dict[str, Any]]:
        self._require_role(actor, ROLE_STEWARD, ROLE_ADMIN)
        key = (participant_id, reason_code, rule_version)
        if key in self._state["exclusion_keys"]:
            return []  # 同一排除理由与规则版本重复提交，沿用原记录
        excluded = self._event(
            "PARTICIPANT_EXCLUDED", "participant", participant_id,
            {"participant_id": participant_id, "reason_code": reason_code,
             "rule_version": rule_version}, occurred_at)
        appended = self._commit([excluded])
        if not appended:
            return []
        plan_ids = self._plans_containing_participant(participant_id)
        cascade = self._cascade(excluded["event_id"], "participant_excluded", plan_ids)
        return appended + cascade

    # ------------------------------------------------------------------ 计划

    def register_plan(self, actor: Mapping[str, str], plan_id: str, cohort_id: str,
                      code_version: str, variable_versions: Mapping[str, str],
                      exclusion_rules: Sequence[str], covariate_scheme_id: str | None = None,
                      occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_STATISTICIAN)
        if plan_id in self._state["plans"]:
            raise RegistryError(f"分析计划 {plan_id} 已登记，计划登记后锁定不可改写")
        if cohort_id not in self._state["cohorts"]:
            raise RegistryError(f"队列快照 {cohort_id} 尚未冻结")
        if self._state["access"].get((actor["subject_id"], cohort_id)) != "granted":
            raise PermissionDenied("登记计划需要该队列名册的访问授权")
        refs = set(variable_versions) | set(exclusion_rules)
        if covariate_scheme_id:
            refs.add(covariate_scheme_id)
        for asset_id in refs:
            if asset_id not in self._state["assets"]:
                raise RegistryError(f"计划引用的资产 {asset_id} 未登记版本")
        event = self._event(
            "PLAN_REGISTERED", "analysis_plan", plan_id,
            {"cohort_id": cohort_id, "code_version": code_version,
             "variable_versions": dict(variable_versions),
             "exclusion_rules": list(exclusion_rules),
             "covariate_scheme_id": covariate_scheme_id,
             "registered_by": actor["subject_id"]}, occurred_at)
        return self._commit([event])[0]

    # ------------------------------------------------------------------ 名额

    def open_quota_pool(self, actor: Mapping[str, str], pool_id: str, capacity: int,
                        occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_ADMIN)
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0:
            raise RegistryError("名额容量必须是非负整数")
        if pool_id in self._state["pools"]:
            raise RegistryError(f"名额池 {pool_id} 已开启")
        event = self._event(
            "QUOTA_POOL_OPENED", "quota_pool", pool_id,
            {"capacity": capacity}, occurred_at)
        return self._commit([event])[0]

    def reserve_slot(self, actor: Mapping[str, str], pool_id: str, holder_id: str,
                     slot_count: int = 1, occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_STATISTICIAN, ROLE_ADMIN)
        with self._lock:
            pool = self._state["pools"].get(pool_id)
            if pool is None:
                raise RegistryError(f"名额池 {pool_id} 不存在")
            if slot_count <= 0:
                raise RegistryError("预留名额数必须为正整数")
            used = sum(pool["held"].values())
            if used + slot_count > pool["capacity"]:
                raise QuotaExhausted(
                    f"名额池 {pool_id} 容量 {pool['capacity']}，已占 {used}，"
                    f"无法再预留 {slot_count}")
            event = self._event(
                "SLOT_RESERVED", "quota_pool", pool_id,
                {"quota_pool_id": pool_id, "holder_id": holder_id,
                 "slot_count": slot_count}, occurred_at)
            return self._commit([event])[0]

    def release_slot(self, actor: Mapping[str, str], pool_id: str, holder_id: str,
                     slot_count: int = 1, occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_STATISTICIAN, ROLE_ADMIN)
        event = self._event(
            "SLOT_RELEASED", "quota_pool", pool_id,
            {"quota_pool_id": pool_id, "holder_id": holder_id,
             "slot_count": slot_count}, occurred_at)
        return self._commit([event])[0]

    # ------------------------------------------------------------------ 运行

    def record_run(self, actor: Mapping[str, str], run_id: str, plan_id: str,
                   run_receipt: str, input_hash: str, parameter_hash: str,
                   occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_STATISTICIAN)
        plan = self._state["plans"].get(plan_id)
        if plan is None:
            raise RegistryError(f"分析计划 {plan_id} 不存在")
        if self._state["access"].get((actor["subject_id"], plan["cohort_id"])) != "granted":
            raise PermissionDenied("记录运行需要该队列名册的访问授权")
        existing_id = self._state["receipts"].get(run_receipt)
        if existing_id is not None:
            existing = self._state["runs"][existing_id]
            if (existing["input_hash"] == input_hash
                    and existing["parameter_hash"] == parameter_hash
                    and existing["plan_id"] == plan_id):
                return next(e for e in self.store.events
                            if e["event_type"] == "RUN_RECORDED"
                            and e["aggregate_id"] == existing_id)
            quarantine_key = (run_receipt, input_hash, parameter_hash, plan_id)
            if quarantine_key not in self._state["quarantine_keys"]:
                quarantine = self._event(
                    "RUN_QUARANTINED", "run_record", run_id,
                    {"run_receipt": run_receipt, "plan_id": plan_id,
                     "reason_code": "fingerprint_mismatch",
                     "existing_run_id": existing_id,
                     "submitted_input_hash": input_hash,
                     "submitted_parameter_hash": parameter_hash},
                    occurred_at)
                self._commit([quarantine])
            raise RegistryError(
                f"运行回执 {run_receipt} 与原记录 {existing_id} 的输入/参数指纹不一致，已隔离")
        event = self._event(
            "RUN_RECORDED", "run_record", run_id,
            {"plan_id": plan_id, "run_receipt": run_receipt,
             "input_hash": input_hash, "parameter_hash": parameter_hash,
             "recorded_by": actor["subject_id"]}, occurred_at)
        return self._commit([event])[0]

    # ------------------------------------------------------------------ 结论审阅与签发

    def review_claim(self, actor: Mapping[str, str], claim_id: str, run_id: str,
                     claim_kind: str, decision: str, comments: str = "",
                     occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_REVIEWER)
        if claim_kind not in CLAIM_KINDS:
            raise RegistryError(f"结论类型必须是 {CLAIM_KINDS} 之一")
        if decision not in ("approved", "changes_requested", "rejected"):
            raise RegistryError("审阅结论必须是 approved / changes_requested / rejected")
        if run_id not in self._state["runs"]:
            raise RegistryError(f"运行 {run_id} 不存在")
        claim = self._state["claims"].get(claim_id)
        if claim is not None and claim.get("status") == "issued":
            raise RegistryError("结论已签发冻结，不能改写审阅意见；如需修订请登记新结论")
        event = self._event(
            "CLAIM_REVIEWED", "research_claim", claim_id,
            {"claim_kind": claim_kind, "reviewer_id": actor["subject_id"],
             "run_id": run_id, "decision": decision, "comments": comments},
            occurred_at)
        return self._commit([event])[0]

    def issue_claim(self, actor: Mapping[str, str], claim_id: str, statement_hash: str,
                    occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_REVIEWER)
        claim = self._state["claims"].get(claim_id)
        if claim is None:
            raise RegistryError(f"结论 {claim_id} 尚无审阅记录")
        if claim.get("status") == "issued":
            raise RegistryError("结论已签发冻结，不可重复签发")
        if not any(r["decision"] == "approved" for r in claim["reviews"]):
            raise RegistryError("结论缺少 approved 科学审阅，不能签发")
        run = self._state["runs"][claim["run_id"]]
        if run["status"] == "invalidated":
            raise RegistryError("结论所依据的运行已被标记失效，请基于重算结果登记新结论")
        plan = self._state["plans"][run["plan_id"]]
        frozen = [
            {"kind": "cohort", "id": plan["cohort_id"]},
            {"kind": "code", "id": plan["code_version"]},
            {"kind": "run", "id": run["run_id"],
             "input_hash": run["input_hash"], "parameter_hash": run["parameter_hash"]},
        ]
        event = self._event(
            "CLAIM_ISSUED", "research_claim", claim_id,
            {"claim_kind": claim["kind"], "run_id": run["run_id"],
             "statement_hash": statement_hash, "frozen_references": frozen},
            occurred_at)
        return self._commit([event])[0]

    # ------------------------------------------------------------------ 公开材料

    def publish_material(self, actor: Mapping[str, str], material_id: str, material_kind: str,
                         frozen_references: Sequence[Mapping[str, str]], title: str = "",
                         occurred_at: str | None = None) -> dict[str, Any]:
        self._require_role(actor, ROLE_ADMIN, ROLE_REVIEWER, ROLE_STATISTICIAN)
        if material_id in self._state["materials"]:
            raise RegistryError(f"公开材料 {material_id} 已发布，历史发表快照不可改写")
        for ref in frozen_references:
            kind, ref_id = ref.get("kind"), ref.get("id")
            if kind == "claim":
                claim = self._state["claims"].get(ref_id)
                if claim is None or claim.get("status") != "issued":
                    raise RegistryError(f"引用的结论 {ref_id} 尚未签发冻结，禁止公开表述")
            elif kind == "run":
                run = self._state["runs"].get(ref_id)
                if run is None or run["status"] != "recorded":
                    raise RegistryError(f"引用的运行 {ref_id} 不是有效冻结版本，禁止公开表述")
            else:
                raise RegistryError("公开材料只能引用已冻结的结论(claim)或运行(run)")
        event = self._event(
            "MATERIAL_PUBLISHED", "public_material", material_id,
            {"material_kind": material_kind, "title": title,
             "frozen_references": list(frozen_references)}, occurred_at)
        return self._commit([event])[0]

    # ------------------------------------------------------------------ 失效级联

    def _plans_referencing_asset(self, asset_id: str) -> list[str]:
        plan_ids = []
        for pid, plan in self._state["plans"].items():
            refs = {plan["cohort_id"], *plan["variable_versions"], *plan["exclusion_rules"]}
            if plan.get("covariate_scheme_id"):
                refs.add(plan["covariate_scheme_id"])
            if asset_id in refs:
                plan_ids.append(pid)
        return plan_ids

    def _plans_containing_participant(self, participant_id: str) -> list[str]:
        result = []
        for pid, plan in self._state["plans"].items():
            members = self._state["assets"].get(plan["cohort_id"], {}).get("participant_ids", set())
            if participant_id in members:
                result.append(pid)
        return result

    def _cascade(self, source_event_id: str, reason_code: str,
                 plan_ids: Sequence[str]) -> list[dict[str, Any]]:
        """沿 计划→运行→结论→公开材料 标记需要重算的产物。

        三级依赖分三批提交，历史事件不删除、不覆盖；失效标记按
        (产物, 来源事件) 去重，多个来源可以分别标记同一产物。
        """
        run_events: list[dict[str, Any]] = []
        affected_run_ids: set[str] = set()
        for pid in plan_ids:
            for run_id in self._state["plans"][pid]["runs"]:
                run = self._state["runs"][run_id]
                key = ("run_record", run_id, source_event_id)
                if run["status"] != "quarantined" and key not in self._state["invalidated"]:
                    run_events.append(self._event(
                        "DEPENDENCY_INVALIDATED", "run_record", run_id,
                        {"reason_code": reason_code, "source_event_id": source_event_id}))
                    affected_run_ids.add(run_id)
        appended_runs = self._commit(run_events)

        claim_events: list[dict[str, Any]] = []
        affected_claim_ids: set[str] = set()
        for cid, claim in self._state["claims"].items():
            if claim.get("run_id") in affected_run_ids:
                key = ("research_claim", cid, source_event_id)
                if key not in self._state["invalidated"]:
                    claim_events.append(self._event(
                        "DEPENDENCY_INVALIDATED", "research_claim", cid,
                        {"reason_code": reason_code, "source_event_id": source_event_id}))
                    affected_claim_ids.add(cid)
        appended_claims = self._commit(claim_events)

        material_events: list[dict[str, Any]] = []
        for mid, material in self._state["materials"].items():
            hit = any((r["kind"] == "claim" and r["id"] in affected_claim_ids)
                      or (r["kind"] == "run" and r["id"] in affected_run_ids)
                      for r in material["frozen_references"])
            if hit and ("public_material", mid, source_event_id) not in self._state["invalidated"]:
                material_events.append(self._event(
                    "DEPENDENCY_INVALIDATED", "public_material", mid,
                    {"reason_code": reason_code, "source_event_id": source_event_id}))
        appended_materials = self._commit(material_events)
        return appended_runs + appended_claims + appended_materials
