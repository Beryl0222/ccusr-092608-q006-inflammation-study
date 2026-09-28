"""端到端演示场景。

用固定标识把一次真实治理流程走一遍，便于联调与命令行追溯：
冻结队列 → 登记六类数据资产 → 两个分析计划与名额池 →
正常运行 / 回执幂等 / 指纹冲突隔离 / 名额不超额 →
结果签发 → 三类科学结论（相关性接受、因果退回、待验证机制接受）→
公开材料必须引用冻结版本 → 参与者撤回与影像质控更正级联标记重算。

另有若干被拒绝的操作，证明角色边界与签发后不可改写。
要求空存储，避免标识冲突。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from .errors import RegistryError
from .registry import (
    DATA_STEWARD,
    PLATFORM_ADMIN,
    SCIENTIFIC_REVIEWER,
    STATISTICIAN,
    Principal,
    Registry,
)

STEWARD = Principal("steward-lin", DATA_STEWARD)
ADMIN = Principal("platform-zhou", PLATFORM_ADMIN)
STAT = Principal("statistician-chen", STATISTICIAN)
REVIEWER = Principal("reviewer-yang", SCIENTIFIC_REVIEWER)


def build_demo(registry: Registry) -> dict[str, Any]:
    if registry.store.events():
        raise RegistryError("演示存储必须为空，请使用新的 JSONL 路径")

    rejected: list[dict[str, str]] = []

    def attempt(description: str, fn: Callable[[], Any]) -> Any | None:
        try:
            return fn()
        except RegistryError as exc:
            rejected.append(
                {"action": description, "field": exc.field, "code": exc.code, "message": exc.message}
            )
            return None

    # 1. 数据管理员冻结两个队列快照
    registry.freeze_cohort(
        STEWARD,
        "cohort-2026-highinfl",
        label="高炎症组主队列（含全量样本编号）",
        sample_refs=[f"P{idx:06d}" for idx in range(1, 11)],
        event_id="evt-cohort-1",
    )
    registry.freeze_cohort(
        STEWARD,
        "cohort-2026-elderly",
        label="老年分层队列",
        sample_refs=[f"E{idx:06d}" for idx in range(1, 6)],
        event_id="evt-cohort-2",
    )

    # 2. 六类数据资产（变量字典/排除理由/标志物批次/影像质控/遗传工具/协变量方案）
    assets = [
        ("dict-vars-3", "variable_dictionary", "sha256:dict-v3"),
        ("excl-standard", "exclusion_reason", "sha256:excl-v2"),
        ("batch-crp-7", "biomarker_batch", "sha256:crp-b7"),
        ("qc-cardiac-mri", "imaging_qc", "sha256:mri-qc-v4"),
        ("iv-il6-gws", "genetic_instrument", "sha256:iv-2026a"),
        ("cov-base", "covariate_scheme", "sha256:cov-v5"),
    ]
    for asset_id, kind, fingerprint in assets:
        registry.register_asset(
            STEWARD,
            asset_id,
            kind,
            fingerprint,
            "cohort-2026-highinfl",
            event_id=f"evt-asset-{asset_id}",
        )

    # 3. 统计人员登记两个分析计划
    registry.register_plan(
        STAT,
        "plan-highinfl-main",
        "cohort-2026-highinfl",
        variable_versions={"crp": "v3.1", "il6": "v2.0", "cardiac_mri_lvef": "qc-v4"},
        exclusion_rules=["missing_crp", "mri_qc_fail", "withdrawn"],
        asset_ids=[a[0] for a in assets],
        event_id="evt-plan-1",
    )
    registry.register_plan(
        STAT,
        "plan-elderly-mri",
        "cohort-2026-elderly",
        variable_versions={"cardiac_mri_lvef": "qc-v4"},
        exclusion_rules=["mri_qc_fail"],
        asset_ids=["qc-cardiac-mri"],
        event_id="evt-plan-2",
    )

    # 4. 名额池：主计划 2 个名额，老年计划 1 个名额
    registry.create_slot_pool(ADMIN, "plan-highinfl-main", 2, event_id="evt-pool-1")
    registry.create_slot_pool(ADMIN, "plan-elderly-mri", 1, event_id="evt-pool-2")
    registry.grant_slot(STAT, "plan-highinfl-main", "alloc-A", event_id="evt-slot-A")
    registry.grant_slot(STAT, "plan-highinfl-main", "alloc-B", event_id="evt-slot-B")
    attempt("主计划申请第三个名额", lambda: registry.grant_slot(STAT, "plan-highinfl-main", "alloc-C"))

    # 5. 主运行 + 回执幂等 + 指纹冲突隔离
    run_inputs = {
        "receipt_id": "receipt-20260928-0001",
        "input_hash": "sha256:input-9f31",
        "parameter_hash": "sha256:params-77aa",
        "code_version": "git:analysis-core@a91c2e7",
        "plan_id": "plan-highinfl-main",
        "cohort_id": "cohort-2026-highinfl",
        "asset_refs": [
            {"asset_id": aid, "fingerprint": fp}
            for aid, _kind, fp in assets
        ],
        "sample_refs": [f"P{idx:06d}" for idx in range(1, 9)],
    }
    registry.record_run(
        STAT, "run-2026-0001", allocation_id="alloc-A", event_id="evt-run-1", **run_inputs
    )
    # 相同回执相同指纹：沿用原记录，不消耗新名额
    repeat = registry.record_run(
        STAT, "run-2026-0001-dup", allocation_id="alloc-A", **run_inputs
    )
    # 相同回执不同指纹：隔离，并消耗 alloc-B
    quarantined = registry.record_run(
        STAT,
        "run-conflicting",
        receipt_id="receipt-20260928-0001",
        allocation_id="alloc-B",
        input_hash="sha256:input-1111",
        parameter_hash="sha256:params-77aa",
        code_version="git:analysis-core@a91c2e7",
        plan_id="plan-highinfl-main",
        cohort_id="cohort-2026-highinfl",
    )
    # 名额已用尽：新回执无名额可用
    attempt(
        "无名额提交第三个运行",
        lambda: registry.record_run(
            STAT,
            "run-2026-0003",
            receipt_id="receipt-20260928-0003",
            allocation_id="alloc-C",
            input_hash="sha256:input-x",
            parameter_hash="sha256:params-x",
            code_version="git:analysis-core@bb",
            plan_id="plan-highinfl-main",
            cohort_id="cohort-2026-highinfl",
        ),
    )

    # 6. 签发主运行结果（冻结）
    registry.issue_result(STAT, "run-2026-0001", "result-2026-0001-v1", event_id="evt-issue-1")
    attempt(
        "签发后改写为另一结果版本",
        lambda: registry.issue_result(STAT, "run-2026-0001", "result-2026-0001-v2"),
    )

    # 7. 科学审阅：相关性接受、因果退回、待验证机制接受
    registry.review_claim(
        REVIEWER,
        "claim-crp-lvef",
        run_id="run-2026-0001",
        claim_kind="correlation_association",
        claim_text="高炎症组 CRP 升高与一年后 LVEF 下降存在相关性（HR 1.18）",
        decision="accepted",
        frozen_result_version="result-2026-0001-v1",
        comment="关联方向与既往研究一致，已校正既定协变量。",
        event_id="evt-claim-1",
    )
    registry.review_claim(
        REVIEWER,
        "claim-il6-causal",
        run_id="run-2026-0001",
        claim_kind="causal_inference",
        claim_text="IL-6 信号是 LVEF 下降的因果驱动因素",
        decision="returned",
        frozen_result_version="result-2026-0001-v1",
        comment="工具变量多效性检验未通过，不能表述为因果。",
        event_id="evt-claim-2",
    )
    attempt(
        "公开被退回的因果结论",
        lambda: registry.publish_material(
            STAT, "mat-press-il6", claim_id="claim-il6-causal", title="新闻稿：炎症因果"
        ),
    )
    registry.review_claim(
        REVIEWER,
        "claim-nlrp3-mech",
        run_id="run-2026-0001",
        claim_kind="mechanism_provisional",
        claim_text="NLRP3 通路可能介导炎症相关心功能下降（待验证机制）",
        decision="accepted",
        frozen_result_version="result-2026-0001-v1",
        comment="仅限待验证机制表述，需独立队列重复。",
        event_id="evt-claim-3",
    )

    # 8. 公开材料引用冻结版本
    registry.publish_material(
        STAT,
        "mat-faq-crp",
        claim_id="claim-crp-lvef",
        title="参与者 FAQ：CRP 与心功能风险表述",
        event_id="evt-mat-1",
    )

    # 9. 老年分层运行，稍后由影像质控更正触发级联
    registry.grant_slot(STAT, "plan-elderly-mri", "alloc-D", event_id="evt-slot-D")
    registry.record_run(
        STAT,
        "run-2026-0002",
        receipt_id="receipt-20260928-0002",
        allocation_id="alloc-D",
        input_hash="sha256:input-elderly-22",
        parameter_hash="sha256:params-elderly-04",
        code_version="git:analysis-core@a91c2e7",
        plan_id="plan-elderly-mri",
        cohort_id="cohort-2026-elderly",
        asset_refs=[{"asset_id": "qc-cardiac-mri", "fingerprint": "sha256:mri-qc-v4"}],
        sample_refs=[f"E{idx:06d}" for idx in range(1, 6)],
        event_id="evt-run-2",
    )
    registry.issue_result(STAT, "run-2026-0002", "result-2026-0002-v1", event_id="evt-issue-2")
    registry.review_claim(
        REVIEWER,
        "claim-elderly-mri",
        run_id="run-2026-0002",
        claim_kind="correlation_association",
        claim_text="老年分层中影像质控通过人群的 LVEF 轨迹与炎症标志物相关",
        decision="accepted",
        frozen_result_version="result-2026-0002-v1",
        comment="依赖 mri-qc-v4；质控版本变更需重算。",
        event_id="evt-claim-4",
    )
    registry.publish_material(
        STAT,
        "mat-slides-elderly",
        claim_id="claim-elderly-mri",
        title="老年分层补充材料幻灯片",
        event_id="evt-mat-2",
    )

    # 10. 越权尝试：统计人员不能冻结队列；数据管理员不能公开材料
    attempt(
        "统计人员冻结队列",
        lambda: registry.freeze_cohort(STAT, "cohort-forbidden", sample_refs=[]),
    )
    attempt(
        "数据管理员公开材料",
        lambda: registry.publish_material(
            STEWARD, "mat-steward-x", claim_id="claim-crp-lvef", title="x"
        ),
    )
    attempt(
        "统计人员自行审阅结论",
        lambda: registry.review_claim(
            STAT,
            "claim-self",
            run_id="run-2026-0001",
            claim_kind="correlation_association",
            claim_text="x",
            decision="accepted",
            frozen_result_version="result-2026-0001-v1",
        ),
    )

    # 11. 参与者 P000001 撤回：沿 run-2026-0001 → 两条接受结论 → FAQ 材料级联
    withdrawal_events = registry.record_withdrawal(
        STEWARD,
        "withdrawal-P000001",
        participant_ref="P000001",
        cohort_id="cohort-2026-highinfl",
        event_id="evt-withdrawal-1",
    )
    # 撤回后旧冻结版本不能支撑新结论或新公开
    attempt(
        "撤回后基于失效运行登记新结论",
        lambda: registry.review_claim(
            REVIEWER,
            "claim-after-withdrawal",
            run_id="run-2026-0001",
            claim_kind="correlation_association",
            claim_text="撤回后的新表述",
            decision="accepted",
            frozen_result_version="result-2026-0001-v1",
        ),
    )
    attempt(
        "撤回后基于失效结论公开新材料",
        lambda: registry.publish_material(
            STAT, "mat-after-withdrawal", claim_id="claim-crp-lvef", title="t"
        ),
    )
    # 撤回重复登记：幂等，不产生第二次级联
    withdrawal_repeat = registry.record_withdrawal(
        STEWARD,
        "withdrawal-P000001-again",
        participant_ref="P000001",
        cohort_id="cohort-2026-highinfl",
    )

    # 12. 影像质控规则更正：新版本指纹，旧指纹产物链全部标记重算
    correction_events = registry.correct_asset(
        STEWARD,
        "qc-cardiac-mri",
        "sha256:mri-qc-v5",
        cohort_id="cohort-2026-elderly",
        event_id="evt-asset-qc-v5",
    )

    events = registry.store.events()
    return {
        "event_count": len(events),
        "events_by_type": dict(sorted(Counter(e["event_type"] for e in events).items())),
        "idempotent_rerun_event_id": repeat["event_id"],
        "quarantined_id": quarantined["aggregate_id"],
        "withdrawal_invalidations": [e["aggregate_id"] for e in withdrawal_events[1:]],
        "withdrawal_repeat_event_count": len(withdrawal_repeat),
        "qc_correction_invalidations": [
            f'{e["aggregate_type"]}/{e["aggregate_id"]}' for e in correction_events[1:]
        ],
        "rejected_actions": rejected,
        "trace_examples": {
            "find_query": "CRP 升高",
            "material_id": "mat-faq-crp",
            "run_id": "run-2026-0001",
        },
    }
