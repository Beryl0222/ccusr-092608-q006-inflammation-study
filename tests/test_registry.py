import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inflammation_study.errors import (
    AuthorizationError,
    ConflictError,
    NotFound,
    QuotaExhausted,
    StateError,
)
from inflammation_study.registry import (
    DATA_STEWARD,
    PLATFORM_ADMIN,
    SCIENTIFIC_REVIEWER,
    STATISTICIAN,
    Principal,
    Registry,
)
from inflammation_study.store import EventStore

STEWARD = Principal("steward-1", DATA_STEWARD)
ADMIN = Principal("admin-1", PLATFORM_ADMIN)
STAT = Principal("stat-1", STATISTICIAN)
REVIEWER = Principal("reviewer-1", SCIENTIFIC_REVIEWER)


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Registry(EventStore(Path(self.tmp.name) / "events.jsonl"))
        self._bootstrap()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _bootstrap(self) -> None:
        r = self.registry
        r.freeze_cohort(
            STEWARD, "cohort-1", label="c1", sample_refs=["P1", "P2", "P3"]
        )
        r.register_asset(STEWARD, "qc-1", "imaging_qc", "fp-qc-v1", "cohort-1")
        r.register_asset(STEWARD, "dict-1", "variable_dictionary", "fp-dict-v1", "cohort-1")
        r.register_plan(
            STAT,
            "plan-1",
            "cohort-1",
            variable_versions={"lvef": "v1"},
            exclusion_rules=["qc_fail"],
            asset_ids=["qc-1", "dict-1"],
        )
        r.create_slot_pool(ADMIN, "plan-1", 2)
        r.grant_slot(STAT, "plan-1", "alloc-1")
        r.grant_slot(STAT, "plan-1", "alloc-2")
        r.record_run(
            STAT,
            "run-1",
            receipt_id="rcpt-1",
            allocation_id="alloc-1",
            input_hash="in-1",
            parameter_hash="pa-1",
            code_version="git:abc",
            plan_id="plan-1",
            cohort_id="cohort-1",
            asset_refs=[
                {"asset_id": "qc-1", "fingerprint": "fp-qc-v1"},
                {"asset_id": "dict-1", "fingerprint": "fp-dict-v1"},
            ],
            sample_refs=["P1", "P2"],
        )
        r.issue_result(STAT, "run-1", "result-v1")
        r.review_claim(
            REVIEWER,
            "claim-1",
            run_id="run-1",
            claim_kind="correlation_association",
            claim_text="高炎症与 LVEF 下降相关",
            decision="accepted",
            frozen_result_version="result-v1",
            comment="证据充分",
        )
        r.publish_material(STAT, "mat-1", claim_id="claim-1", title="FAQ 风险表述")

    # ------------------------------------------------------------ 权限

    def test_role_boundaries(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.registry.freeze_cohort(STAT, "cohort-x")
        with self.assertRaises(AuthorizationError):
            self.registry.register_plan(STEWARD, "plan-x", "cohort-1", variable_versions={}, exclusion_rules=[])
        with self.assertRaises(AuthorizationError):
            self.registry.review_claim(
                STAT, "claim-x", run_id="run-1", claim_kind="causal_inference",
                claim_text="x", decision="accepted", frozen_result_version="result-v1",
            )
        with self.assertRaises(AuthorizationError):
            self.registry.publish_material(STEWARD, "mat-x", claim_id="claim-1", title="x")
        with self.assertRaises(AuthorizationError):
            self.registry.create_slot_pool(STAT, "plan-1", 1)
        with self.assertRaises(AuthorizationError):
            self.registry.record_withdrawal(STAT, "w-1", participant_ref="P1", cohort_id="cohort-1")

    # ------------------------------------------------------------ 幂等/隔离

    def test_run_receipt_idempotent(self) -> None:
        before = len(self.registry.store.events())
        result = self.registry.record_run(
            STAT,
            "run-1-dup",
            receipt_id="rcpt-1",
            allocation_id="alloc-1",  # 已释放也允许，因为不会再消耗
            input_hash="in-1",
            parameter_hash="pa-1",
            code_version="git:abc",
            plan_id="plan-1",
            cohort_id="cohort-1",
        )
        self.assertEqual(result["aggregate_id"], "run-1")
        self.assertEqual(len(self.registry.store.events()), before)

    def test_run_receipt_fingerprint_mismatch_is_quarantined(self) -> None:
        result = self.registry.record_run(
            STAT,
            "run-bad",
            receipt_id="rcpt-1",
            allocation_id="alloc-2",
            input_hash="in-DIFFERENT",
            parameter_hash="pa-1",
            code_version="git:abc",
            plan_id="plan-1",
            cohort_id="cohort-1",
        )
        self.assertEqual(result["event_type"], "RUN_QUARANTINED")
        self.assertTrue(result["aggregate_id"].startswith("quarantine:rcpt-1:"))
        original = next(
            e for e in self.registry.store.events() if e["aggregate_id"] == "run-1"
        )
        self.assertEqual(result["payload"]["stored_event_id"], original["event_id"])
        self.assertEqual(result["payload"]["stored_input_hash"], "in-1")
        # 原记录保持不动，仍可签发与追溯
        self.assertIsNotNone(original)

    def test_quarantine_resubmission_is_idempotent(self) -> None:
        kwargs = dict(
            receipt_id="rcpt-1",
            allocation_id="alloc-2",
            input_hash="in-DIFFERENT",
            parameter_hash="pa-1",
            code_version="git:abc",
            plan_id="plan-1",
            cohort_id="cohort-1",
        )
        first = self.registry.record_run(STAT, "run-bad", **kwargs)
        # 完全相同的冲突提交重放：沿用原隔离记录
        second = self.registry.record_run(STAT, "run-bad2", **kwargs)
        self.assertEqual(first["event_id"], second["event_id"])

    # ------------------------------------------------------------ 名额

    def test_slots_cannot_exceed_capacity(self) -> None:
        self.registry.register_plan(
            STAT, "plan-2", "cohort-1", variable_versions={}, exclusion_rules=[]
        )
        self.registry.create_slot_pool(ADMIN, "plan-2", 1)
        self.registry.grant_slot(STAT, "plan-2", "a")
        with self.assertRaises(QuotaExhausted):
            self.registry.grant_slot(STAT, "plan-2", "b")

    def test_grant_slot_idempotent_and_used_slot_not_regrantable(self) -> None:
        # alloc-2 已授予但未使用：重复申请沿用原授予
        g = self.registry.grant_slot(STAT, "plan-1", "alloc-2")
        self.assertEqual(g["payload"]["allocation_id"], "alloc-2")
        # alloc-1 已被 run-1 使用（释放）：不能重新授予
        with self.assertRaises(StateError):
            self.registry.grant_slot(STAT, "plan-1", "alloc-1")

    def test_run_without_grant_rejected(self) -> None:
        with self.assertRaises(StateError):
            self.registry.record_run(
                STAT, "run-x", receipt_id="rcpt-x", allocation_id="alloc-NONE",
                input_hash="i", parameter_hash="p", code_version="g",
                plan_id="plan-1", cohort_id="cohort-1",
            )

    def test_concurrent_grants_never_oversell(self) -> None:
        self.registry.register_plan(
            STAT, "plan-c", "cohort-1", variable_versions={}, exclusion_rules=[]
        )
        self.registry.create_slot_pool(ADMIN, "plan-c", 5)

        def grant(idx: int) -> bool:
            try:
                self.registry.grant_slot(STAT, "plan-c", f"c-{idx}")
                return True
            except QuotaExhausted:
                return False

        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(pool.map(grant, range(20)))
        self.assertEqual(sum(outcomes), 5)

    # ------------------------------------------------------------ 签发/审阅/公开

    def test_issued_result_is_frozen(self) -> None:
        with self.assertRaises(StateError):
            self.registry.issue_result(STAT, "run-1", "result-v2")
        # 重复同版本签发是幂等
        again = self.registry.issue_result(STAT, "run-1", "result-v1")
        self.assertEqual(again["payload"]["result_version"], "result-v1")

    def test_review_must_reference_frozen_version(self) -> None:
        with self.assertRaises(ConflictError):
            self.registry.review_claim(
                REVIEWER, "claim-bad", run_id="run-1",
                claim_kind="causal_inference", claim_text="x", decision="accepted",
                frozen_result_version="result-OTHER",
            )

    def test_returned_claim_cannot_be_published(self) -> None:
        self.registry.review_claim(
            REVIEWER, "claim-ret", run_id="run-1",
            claim_kind="causal_inference", claim_text="因果表述",
            decision="returned", frozen_result_version="result-v1",
        )
        with self.assertRaises(StateError):
            self.registry.publish_material(STAT, "mat-ret", claim_id="claim-ret", title="t")

    def test_claim_id_cannot_be_reused(self) -> None:
        with self.assertRaises(ConflictError):
            self.registry.review_claim(
                REVIEWER, "claim-1", run_id="run-1",
                claim_kind="mechanism_provisional", claim_text="x", decision="accepted",
                frozen_result_version="result-v1",
            )

    def test_reject_unknown_kind(self) -> None:
        with self.assertRaises(StateError):
            self.registry.review_claim(
                REVIEWER, "claim-k", run_id="run-1",
                claim_kind="headline", claim_text="x", decision="accepted",
                frozen_result_version="result-v1",
            )

    # ------------------------------------------------------------ 撤回/级联

    def test_withdrawal_cascades_and_preserves_history(self) -> None:
        events_before = len(self.registry.store.events())
        result = self.registry.record_withdrawal(
            STEWARD, "w-1", participant_ref="P1", cohort_id="cohort-1"
        )
        invalidated = {(e["aggregate_type"], e["aggregate_id"]) for e in result[1:]}
        self.assertIn(("run_record", "run-1"), invalidated)
        self.assertIn(("research_claim", "claim-1"), invalidated)
        self.assertIn(("public_material", "mat-1"), invalidated)

        # 历史事件全部保留
        self.assertGreater(len(self.registry.store.events()), events_before)
        self.assertTrue(
            any(e["event_type"] == "MATERIAL_PUBLISHED" for e in self.registry.store.events())
        )
        # 失效后不能再签发新结论版本、不能公开新材料
        with self.assertRaises(StateError):
            self.registry.review_claim(
                REVIEWER, "claim-2", run_id="run-1",
                claim_kind="correlation_association", claim_text="新表述",
                decision="accepted", frozen_result_version="result-v1",
            )
        with self.assertRaises(StateError):
            self.registry.publish_material(STAT, "mat-2", claim_id="claim-1", title="t")

    def test_withdrawal_is_idempotent(self) -> None:
        first = self.registry.record_withdrawal(
            STEWARD, "w-1", participant_ref="P1", cohort_id="cohort-1"
        )
        second = self.registry.record_withdrawal(
            STEWARD, "w-1-again", participant_ref="P1", cohort_id="cohort-1"
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["event_id"], first[0]["event_id"])

    def test_withdrawal_only_affects_runs_using_participant(self) -> None:
        # P3 不在 run-1 的 sample_refs 中
        result = self.registry.record_withdrawal(
            STEWARD, "w-3", participant_ref="P3", cohort_id="cohort-1"
        )
        self.assertEqual(len(result), 1)
        self.assertIsNone(self.registry._invalidation(
            self.registry._state().history("run_record", "run-1")
        ))

    def test_qc_correction_cascades_to_downstream(self) -> None:
        result = self.registry.correct_asset(
            STEWARD, "qc-1", "fp-qc-v2", cohort_id="cohort-1"
        )
        ids = {(e["aggregate_type"], e["aggregate_id"]) for e in result[1:]}
        self.assertEqual(
            ids,
            {
                ("run_record", "run-1"),
                ("research_claim", "claim-1"),
                ("public_material", "mat-1"),
            },
        )
        # 资产新版本已登记，同指纹再次“更正”为幂等
        again = self.registry.correct_asset(STEWARD, "qc-1", "fp-qc-v2", cohort_id="cohort-1")
        self.assertEqual(len(again), 1)

    def test_recompute_chain_after_correction(self) -> None:
        # 更正后用新指纹重跑 → 签发新结果版本 → 新结论 → 新材料
        self.registry.correct_asset(STEWARD, "qc-1", "fp-qc-v2", cohort_id="cohort-1")
        self.registry.register_plan(
            STAT, "plan-rerun", "cohort-1",
            variable_versions={"lvef": "v2"}, exclusion_rules=["qc_fail"],
            asset_ids=["qc-1"],
        )
        self.registry.create_slot_pool(ADMIN, "plan-rerun", 1)
        self.registry.grant_slot(STAT, "plan-rerun", "alloc-r1")
        self.registry.record_run(
            STAT, "run-rerun", receipt_id="rcpt-r1", allocation_id="alloc-r1",
            input_hash="in-2", parameter_hash="pa-2", code_version="git:def",
            plan_id="plan-rerun", cohort_id="cohort-1",
            asset_refs=[{"asset_id": "qc-1", "fingerprint": "fp-qc-v2"}],
            sample_refs=["P1", "P2"],
        )
        self.registry.issue_result(STAT, "run-rerun", "result-v2")
        self.registry.review_claim(
            REVIEWER, "claim-rerun", run_id="run-rerun",
            claim_kind="correlation_association", claim_text="重算后的关联",
            decision="accepted", frozen_result_version="result-v2",
        )
        material = self.registry.publish_material(
            STAT, "mat-rerun", claim_id="claim-rerun", title="修订版 FAQ"
        )
        self.assertEqual(material["payload"]["frozen_result_version"], "result-v2")

    # ------------------------------------------------------------ 谱系与裁剪

    def test_find_and_trace_from_claim_text(self) -> None:
        self.assertEqual(self.registry.find_claims_by_text("LVEF"), ["claim-1"])
        view = self.registry.trace("claim-1", ADMIN)
        self.assertEqual(view["run"]["id"], "run-1")
        self.assertEqual(view["run"]["code_version"], "git:abc")
        self.assertEqual(view["plan"]["variable_versions"], {"lvef": "v1"})
        self.assertEqual(view["cohort"]["id"], "cohort-1")
        self.assertEqual(view["run"]["issued"]["result_version"], "result-v1")
        self.assertEqual(view["run"]["parameter_hash"], "pa-1")
        self.assertIn("comment", view["claim"])
        self.assertEqual(view["downstream_materials"][0]["material_id"], "mat-1")
        self.assertIn("event_log", view)

    def test_trace_minimal_views_by_role(self) -> None:
        admin = self.registry.trace("mat-1", ADMIN)
        public = self.registry.trace("mat-1", Principal("anon", "public"))
        reviewer = self.registry.trace("mat-1", REVIEWER)
        statistician = self.registry.trace("mat-1", STAT)
        steward = self.registry.trace("run-1", STEWARD)

        # 公开视角：看得到冻结版本与失效状态，看不到指纹、审阅人、样本编号
        self.assertEqual(public["material"]["frozen_result_version"], "result-v1")
        self.assertNotIn("reviewer_id", public["claim"])
        self.assertNotIn("input_hash", public["run"])
        self.assertNotIn("sample_refs", public["run"])
        self.assertNotIn("variable_versions", public["plan"])
        self.assertNotIn("withdrawals", public)
        self.assertNotIn("event_log", public)

        # 审阅者：看得到意见，看不到技术指纹与样本
        self.assertIn("comment", reviewer["claim"])
        self.assertNotIn("input_hash", reviewer["run"])
        self.assertNotIn("sample_refs", reviewer["run"])

        # 统计人员：技术字段与审阅意见可见，样本不可见
        self.assertIn("parameter_hash", statistician["run"])
        self.assertIn("comment", statistician["claim"])
        self.assertNotIn("sample_refs", statistician["run"])
        self.assertNotIn("withdrawals", statistician)

        # 数据管理员：样本与撤回可见，指纹、审阅意见不可见
        self.assertIn("sample_refs", steward["run"])
        self.assertNotIn("input_hash", steward["run"])
        self.assertNotIn("comment", steward.get("claim", {}))

        # 主管能看到全部，且 event_log 覆盖链路上各聚合
        admin_ids = {e["event_id"] for e in admin["event_log"]}
        claim_event_id = next(
            e["event_id"] for e in self.registry.store.events()
            if e["aggregate_type"] == "research_claim" and e["aggregate_id"] == "claim-1"
        )
        self.assertIn(claim_event_id, admin_ids)

    def test_public_cannot_trace_internal_run(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.registry.trace("run-1", Principal("anon", "public"))

    def test_trace_unknown_target(self) -> None:
        with self.assertRaises(NotFound):
            self.registry.trace("nope", ADMIN)

    def test_trace_marks_invalidation_reason(self) -> None:
        self.registry.record_withdrawal(STEWARD, "w-1", participant_ref="P1", cohort_id="cohort-1")
        view = self.registry.trace("mat-1", Principal("anon", "public"))
        self.assertEqual(view["status"], "invalidated")
        self.assertEqual(view["run"]["invalidation"]["reason_kind"], "participant_withdrawal")
        self.assertEqual(view["claim"]["invalidation"]["reason_kind"], "upstream_invalidation")

    def test_asset_staleness_in_trace(self) -> None:
        self.registry.correct_asset(STEWARD, "qc-1", "fp-qc-v2", cohort_id="cohort-1")
        admin = self.registry.trace("run-1", ADMIN)
        qc = next(a for a in admin["assets"] if a["asset_id"] == "qc-1")
        self.assertTrue(qc["stale"])
        self.assertEqual(qc["used_fingerprint"], "fp-qc-v1")
        self.assertEqual(qc["current_fingerprint"], "fp-qc-v2")
        public = self.registry.trace("mat-1", Principal("anon", "public"))
        qc_public = next(a for a in public["assets"] if a["asset_id"] == "qc-1")
        self.assertNotIn("used_fingerprint", qc_public)
        self.assertTrue(qc_public["stale"])


if __name__ == "__main__":
    unittest.main()
