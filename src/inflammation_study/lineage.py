"""按权限投影的最小数据谱系。

从一条已签发结论（风险表述）向上追溯到队列快照、代码版本、变量/排除/
协变量资产、运行输入与参数指纹、科学审阅意见；向下列出受同一失效来源
影响、需要重算的后续产物与公开材料。

最小化原则：

- 调用方只能看到与该结论相关的产物，不暴露全库。
- 资产指纹/名册成员等敏感字段仅对有该资产访问授权的角色开放，
  无授权时只给资产标识与 `access_required` 掩码。
- 统计人员视角不返回参与者标识，撤回/排除来源以参与者序号与计数表达。
"""

from __future__ import annotations

from typing import Any, Mapping

from .registry import (ROLE_ADMIN, ROLE_REVIEWER, ROLE_STEWARD, Registry,
                       PermissionDenied)

_PRIVILEGED_ROLES = (ROLE_ADMIN, ROLE_STEWARD, ROLE_REVIEWER)


class LineageDenied(PermissionDenied):
    """无权查看该结论的谱系。"""


class LineageView:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def _can_read(self, actor: Mapping[str, str], asset_id: str) -> bool:
        if actor.get("role") in _PRIVILEGED_ROLES:
            return True
        return self.registry._state["access"].get(
            (actor.get("subject_id"), asset_id)) == "granted"

    def _masked_asset(self, actor: Mapping[str, str], asset_id: str,
                      version: Any = None) -> dict[str, Any]:
        if not self._can_read(actor, asset_id):
            return {"asset_id": asset_id, "access": "access_required"}
        asset = self.registry._state["assets"].get(asset_id, {})
        view: dict[str, Any] = {
            "asset_id": asset_id,
            "access": "granted",
            "asset_kind": asset.get("kind"),
            "asset_version": version or asset.get("version"),
        }
        if actor.get("role") in _PRIVILEGED_ROLES:
            view["fingerprint"] = asset.get("fingerprint")
        else:
            # 统计人员只需要确认指纹与登记时一致，给短指纹即可。
            fp = asset.get("fingerprint")
            view["fingerprint_short"] = fp[:12] if isinstance(fp, str) else None
        return view

    def for_claim(self, actor: Mapping[str, str], claim_id: str) -> dict[str, Any]:
        state = self.registry._state
        claim = state["claims"].get(claim_id)
        if claim is None:
            raise LineageDenied(f"结论 {claim_id} 不存在")
        run = state["runs"].get(claim["run_id"])
        if run is None:
            raise LineageDenied(f"结论 {claim_id} 缺少运行记录")
        plan = state["plans"].get(run["plan_id"])
        if plan is None:
            raise LineageDenied(f"运行 {run['run_id']} 缺少分析计划")
        cohort_id = plan["cohort_id"]
        if not self._can_read(actor, cohort_id):
            raise LineageDenied("无权查看该结论所依据的队列")

        cohort = state["cohorts"][cohort_id]
        roster = state["assets"].get(cohort_id, {})
        roster_members = sorted(roster.get("participant_ids", ()))
        cohort_view = {
            "cohort_id": cohort_id,
            "frozen_at": cohort["frozen_at"],
            "definition_hash_short": cohort["definition_hash"][:12],
            "roster_size": len(roster_members),
        }
        if actor.get("role") in _PRIVILEGED_ROLES:
            cohort_view["withdrawn_in_roster"] = sorted(
                state["withdrawn"] & set(roster_members))
            cohort_view["excluded_in_roster"] = {
                pid: state["exclusions"][pid]
                for pid in roster_members if pid in state["exclusions"]
            }

        variable_views = {}
        for asset_id, pinned in sorted(plan["variable_versions"].items()):
            view = self._masked_asset(actor, asset_id, pinned)
            variable_views[asset_id] = view
        exclusion_views = [self._masked_asset(actor, asset_id)
                           for asset_id in plan["exclusion_rules"]]
        covariate_view = None
        if plan.get("covariate_scheme_id"):
            covariate_view = self._masked_asset(actor, plan["covariate_scheme_id"])

        run_view: dict[str, Any] = {
            "run_id": run["run_id"],
            "run_receipt": run["run_receipt"],
            "input_hash": run["input_hash"] if actor.get("role") in _PRIVILEGED_ROLES
            else run["input_hash"][:12],
            "parameter_hash": run["parameter_hash"] if actor.get("role") in _PRIVILEGED_ROLES
            else run["parameter_hash"][:12],
            "recorded_by": run.get("recorded_by"),
            "recorded_at": run.get("recorded_at"),
            "status": run["status"],
        }
        claim_view = {
            "claim_id": claim_id,
            "claim_kind": claim["kind"],
            "status": claim["status"],
            "statement_hash": claim.get("statement_hash"),
            "issued_at": claim.get("issued_at"),
            "reviews": claim.get("reviews", []),
            "frozen_references": claim.get("frozen_references", []),
        }

        # 失效来源（撤回/排除/质控更正）经运行层向上暴露
        source_events = {inv["source_event_id"] for inv in claim.get("invalidations", [])}
        invalidation_sources = [self._source_view(actor, eid)
                                for eid in sorted(source_events)]
        invalidation_sources = [v for v in invalidation_sources if v is not None]

        # 受影响的后续公开材料（引用了该结论或其运行）
        affected_materials = []
        for mid, material in sorted(state["materials"].items()):
            refs = material["frozen_references"]
            if any((r["kind"] == "claim" and r["id"] == claim_id)
                   or (r["kind"] == "run" and r["id"] == run["run_id"])
                   for r in refs):
                affected_materials.append({
                    "material_id": mid,
                    "material_kind": material["kind"],
                    "title": material.get("title"),
                    "published_at": material["published_at"],
                    "status": material["status"],
                })

        return {
            "claim": claim_view,
            "run": run_view,
            "plan": {
                "plan_id": run["plan_id"],
                "code_version": plan["code_version"],
                "registered_by": plan.get("registered_by"),
                "registered_at": plan.get("registered_at"),
                "variables": variable_views,
                "exclusion_rules": exclusion_views,
                "covariate_scheme": covariate_view,
            },
            "cohort": cohort_view,
            "invalidation_sources": invalidation_sources,
            "affected_materials": affected_materials,
        }

    def _source_view(self, actor: Mapping[str, str],
                     source_event_id: str) -> dict[str, Any] | None:
        event = next((e for e in self.registry.store.events
                      if e["event_id"] == source_event_id), None)
        if event is None:
            return None
        et = event["event_type"]
        body = event.get("payload", {})
        view = {
            "source_event_id": source_event_id,
            "source_event_type": et,
            "occurred_at": event["occurred_at"],
        }
        if et in ("WITHDRAWAL_RECEIVED", "PARTICIPANT_EXCLUDED"):
            if actor.get("role") in _PRIVILEGED_ROLES:
                view["participant_id"] = body["participant_id"]
                if "reason_code" in body:
                    view["reason_code"] = body["reason_code"]
                    view["rule_version"] = body["rule_version"]
            else:
                view["participant"] = "redacted"
                view["note"] = "参与者层面细节仅对数据管理员与科学审阅者开放"
        elif et == "QC_RULE_CORRECTED":
            view["asset_id"] = body["asset_id"]
            view["old_version"] = body["old_version"]
            view["new_version"] = body["new_version"]
        return view
