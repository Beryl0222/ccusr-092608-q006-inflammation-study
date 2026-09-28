import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inflammation_study.events import EventConflict, EventStore
from inflammation_study.lineage import LineageDenied, LineageView
from inflammation_study.registry import (PermissionDenied, QuotaExhausted,
                                         Registry, RegistryError,
                                         ROLE_ADMIN, ROLE_REVIEWER,
                                         ROLE_STATISTICIAN, ROLE_STEWARD)

ADMIN = {"subject_id": "admin1", "role": ROLE_ADMIN}
STEWARD = {"subject_id": "dm1", "role": ROLE_STEWARD}
STAT = {"subject_id": "stat1", "role": ROLE_STATISTICIAN}
STAT2 = {"subject_id": "stat2", "role": ROLE_STATISTICIAN}
REVIEWER = {"subject_id": "rev1", "role": ROLE_REVIEWER}


def build_registry(path=None, members=("p1", "p2", "p3")):
    schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
    reg = Registry(EventStore(path=path, schema=schema))
    reg.freeze_cohort(ADMIN, "cohort-1", "hash-cohort", list(members))
    reg.version_asset(STEWARD, "vars-1", "variable_dictionary", "hash-vars")
    reg.version_asset(STEWARD, "excl-1", "exclusion_rule_set", "hash-excl")
    reg.version_asset(STEWARD, "cov-1", "covariate_scheme", "hash-cov")
    reg.version_asset(STEWARD, "qc-1", "imaging_qc", "hash-qc-v1")
    reg.grant_access(STEWARD, "stat1", "cohort-1")
    reg.register_plan(
        STAT, "plan-1", "cohort-1", "git:abc123",
        {"vars-1": "1"}, ["excl-1"], covariate_scheme_id="cov-1")
    return reg


def issue_happy_claim(reg, run_id="run-1", claim_id="claim-1", kind="association"):
    reg.open_quota_pool(ADMIN, "pool-1", 2)
    reg.reserve_slot(STAT, "pool-1", "stat1")
    reg.record_run(STAT, run_id, "plan-1", f"rcpt-{run_id}", "input-1", "param-1")
    reg.review_claim(REVIEWER, claim_id, run_id, kind, "approved", "相关性表述与数据一致")
    reg.issue_claim(REVIEWER, claim_id, "stmt-hash-1")
    reg.publish_material(
        ADMIN, "mat-1", "paper", [{"kind": "claim", "id": claim_id}], title="修订稿表2")


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry()

    def test_statistician_cannot_freeze_cohort(self):
        with self.assertRaises(PermissionDenied):
            self.reg.freeze_cohort(STAT, "cohort-x", "h", ["p"])

    def test_steward_cannot_register_plan(self):
        with self.assertRaises(PermissionDenied):
            self.reg.register_plan(
                STEWARD, "plan-x", "cohort-1", "git:x", {"vars-1": "1"}, ["excl-1"])

    def test_statistician_cannot_manage_access(self):
        with self.assertRaises(PermissionDenied):
            self.reg.grant_access(STAT, "stat2", "cohort-1")

    def test_statistician_cannot_review(self):
        self.reg.record_run(STAT, "run-1", "plan-1", "r", "i", "p")
        with self.assertRaises(PermissionDenied):
            self.reg.review_claim(STAT, "c", "run-1", "association", "approved")

    def test_plan_requires_cohort_access(self):
        with self.assertRaises(PermissionDenied):
            self.reg.register_plan(
                STAT2, "plan-2", "cohort-1", "git:abc", {"vars-1": "1"}, ["excl-1"])

    def test_steward_only_handles_access_and_withdrawal(self):
        # 数据管理员不能登记运行
        with self.assertRaises(PermissionDenied):
            self.reg.record_run(STEWARD, "run-x", "plan-1", "r", "i", "p")
        # 但撤回与授权是其职责
        self.reg.grant_access(STEWARD, "x", "vars-1")
        self.assertEqual(
            self.reg.receive_withdrawal(STEWARD, "p1")[0]["event_type"],
            "WITHDRAWAL_RECEIVED")


class LockingTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry()
        issue_happy_claim(self.reg)

    def test_plan_is_locked_after_registration(self):
        with self.assertRaises(RegistryError):
            self.reg.register_plan(
                STAT, "plan-1", "cohort-1", "git:xxx", {"vars-1": "1"}, ["excl-1"])

    def test_cohort_snapshot_is_immutable(self):
        with self.assertRaises(RegistryError):
            self.reg.freeze_cohort(ADMIN, "cohort-1", "other", ["p1"])

    def test_issued_claim_cannot_be_rewritten(self):
        with self.assertRaises(RegistryError):
            self.reg.review_claim(
                REVIEWER, "claim-1", "run-1", "causal", "approved", "改成因果")
        with self.assertRaises(RegistryError):
            self.reg.issue_claim(REVIEWER, "claim-1", "stmt-hash-2")

    def test_published_material_snapshot_immutable(self):
        with self.assertRaises(RegistryError):
            self.reg.publish_material(
                ADMIN, "mat-1", "paper", [{"kind": "claim", "id": "claim-1"}])

    def test_unknown_claim_kind_rejected(self):
        with self.assertRaises(RegistryError):
            self.reg.review_claim(
                REVIEWER, "c9", "run-1", "guaranteed_truth", "approved")


class RunReceiptTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry()

    def test_same_receipt_same_fingerprints_reuses_record(self):
        first = self.reg.record_run(STAT, "run-1", "plan-1", "rcpt", "in", "pm")
        again = self.reg.record_run(STAT, "run-1", "plan-1", "rcpt", "in", "pm")
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual(1, sum(1 for e in self.reg.store.events
                                if e["event_type"] == "RUN_RECORDED"))

    def test_fingerprint_mismatch_is_quarantined_not_overwritten(self):
        self.reg.record_run(STAT, "run-1", "plan-1", "rcpt", "in", "pm")
        with self.assertRaises(RegistryError):
            self.reg.record_run(STAT, "run-bad", "plan-1", "rcpt", "in2", "pm")
        # 原运行保持有效，冲突提交单独隔离
        self.assertEqual("recorded", self.reg._state["runs"]["run-1"]["status"])
        self.assertEqual("quarantined", self.reg._state["runs"]["run-bad"]["status"])
        quarantined = [e for e in self.reg.store.events
                       if e["event_type"] == "RUN_QUARANTINED"]
        self.assertEqual(1, len(quarantined))
        self.assertEqual("fingerprint_mismatch", quarantined[0]["payload"]["reason_code"])

    def test_repeated_mismatch_submission_does_not_duplicate(self):
        self.reg.record_run(STAT, "run-1", "plan-1", "rcpt", "in", "pm")
        for rid in ("run-bad", "run-bad2"):
            with self.assertRaises(RegistryError):
                self.reg.record_run(STAT, rid, "plan-1", "rcpt", "in2", "pm")
        self.assertEqual(1, sum(1 for e in self.reg.store.events
                                if e["event_type"] == "RUN_QUARANTINED"))


class QuotaTests(unittest.TestCase):
    def test_concurrent_reservations_never_exceed_capacity(self):
        reg = build_registry()
        reg.open_quota_pool(ADMIN, "pool-c", 10)
        outcomes = []
        lock = threading.Lock()

        def worker(i):
            try:
                reg.reserve_slot(
                    {"subject_id": f"u{i}", "role": ROLE_STATISTICIAN},
                    "pool-c", f"u{i}")
                with lock:
                    outcomes.append("ok")
            except QuotaExhausted:
                with lock:
                    outcomes.append("full")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(10, outcomes.count("ok"))
        self.assertEqual(90, outcomes.count("full"))
        self.assertEqual(10, sum(reg._state["pools"]["pool-c"]["held"].values()))

    def test_release_frees_capacity(self):
        reg = build_registry()
        reg.open_quota_pool(ADMIN, "pool", 1)
        reg.reserve_slot(STAT, "pool", "stat1")
        with self.assertRaises(QuotaExhausted):
            reg.reserve_slot(STAT2, "pool", "stat2")
        reg.release_slot(STAT, "pool", "stat1")
        reg.reserve_slot(STAT2, "pool", "stat2")
        self.assertEqual(1, sum(reg._state["pools"]["pool"]["held"].values()))


class CascadeTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry()
        issue_happy_claim(self.reg)

    def test_withdrawal_cascades_but_history_remains(self):
        appended = self.reg.receive_withdrawal(STEWARD, "p2")
        kinds = [(e["event_type"], e["aggregate_type"], e["aggregate_id"]) for e in appended]
        self.assertIn(("DEPENDENCY_INVALIDATED", "run_record", "run-1"), kinds)
        self.assertIn(("DEPENDENCY_INVALIDATED", "research_claim", "claim-1"), kinds)
        self.assertIn(("DEPENDENCY_INVALIDATED", "public_material", "mat-1"), kinds)
        self.assertEqual("invalidated", self.reg._state["runs"]["run-1"]["status"])
        self.assertEqual("invalidated", self.reg._state["claims"]["claim-1"]["status"])
        self.assertEqual("invalidated", self.reg._state["materials"]["mat-1"]["status"])
        # 历史签发/发表快照仍在日志中
        self.assertTrue(any(e["event_type"] == "CLAIM_ISSUED"
                            for e in self.reg.store.events))
        self.assertTrue(any(e["event_type"] == "MATERIAL_PUBLISHED"
                            for e in self.reg.store.events))

    def test_withdrawal_is_idempotent(self):
        first = self.reg.receive_withdrawal(STEWARD, "p2")
        second = self.reg.receive_withdrawal(STEWARD, "p2")
        self.assertTrue(first)
        self.assertEqual([], second)

    def test_exclusion_cascades_with_distinct_reason(self):
        self.reg.exclude_participant(STEWARD, "p2", "imaging_qc_fail", "qc-1@1")
        run = self.reg._state["runs"]["run-1"]
        self.assertEqual(1, len(run["invalidations"]))
        # 同理由同规则版本重复提交沿用原记录
        again = self.reg.exclude_participant(STEWARD, "p2", "imaging_qc_fail", "qc-1@1")
        self.assertEqual([], again)
        self.assertEqual(1, len(run["invalidations"]))

    def test_qc_correction_only_cascades_referencing_plans(self):
        # qc-1 未被 plan-1 引用，更正不应标记其产物
        self.reg.correct_qc_rule(STEWARD, "qc-1", "hash-qc-v2")
        self.assertEqual("recorded", self.reg._state["runs"]["run-1"]["status"])
        # 新计划引用 qc-1 后，再次更正应当级联
        self.reg.version_asset(STEWARD, "vars-2", "variable_dictionary", "hash-vars2")
        self.reg.grant_access(STEWARD, "stat1", "cohort-1")
        self.reg.register_plan(
            STAT, "plan-2", "cohort-1", "git:def456",
            {"vars-2": "1"}, ["excl-1", "qc-1"])
        self.reg.record_run(STAT, "run-2", "plan-2", "rcpt-2", "in2", "pm2")
        self.reg.correct_qc_rule(STEWARD, "qc-1", "hash-qc-v3")
        self.assertEqual("recorded", self.reg._state["runs"]["run-1"]["status"])
        self.assertEqual("invalidated", self.reg._state["runs"]["run-2"]["status"])

    def test_cannot_issue_claim_on_invalidated_run(self):
        self.reg.receive_withdrawal(STEWARD, "p2")
        self.reg.review_claim(REVIEWER, "claim-2", "run-1", "causal",
                              "approved", "尝试基于失效运行签发")
        with self.assertRaises(RegistryError):
            self.reg.issue_claim(REVIEWER, "claim-2", "stmt-2")


class PublishingRulesTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry()

    def test_material_must_reference_frozen_claim(self):
        with self.assertRaises(RegistryError):
            self.reg.publish_material(
                ADMIN, "mat-x", "press_release", [{"kind": "claim", "id": "nope"}])

    def test_issue_requires_approved_review(self):
        self.reg.record_run(STAT, "run-1", "plan-1", "r", "i", "p")
        self.reg.review_claim(REVIEWER, "claim-1", "run-1", "causal",
                              "changes_requested", "因果证据不足")
        with self.assertRaises(RegistryError):
            self.reg.issue_claim(REVIEWER, "claim-1", "s")


class EventStoreTests(unittest.TestCase):
    def test_event_id_conflict_is_isolated(self):
        store = EventStore()
        event = {"event_id": "e1", "event_type": "COHORT_FROZEN",
                 "aggregate_type": "cohort_snapshot", "aggregate_id": "c1",
                 "occurred_at": "2026-09-25T10:00:00+08:00",
                 "payload": {"definition_hash": "a"}}
        store.append(event)
        mutated = dict(event, payload={"definition_hash": "b"})
        with self.assertRaises(EventConflict):
            store.append(mutated)

    def test_jsonl_replay_rebuilds_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            reg = build_registry(path=path)
            issue_happy_claim(reg)
            reg.receive_withdrawal(STEWARD, "p2")
            replayed = Registry(EventStore(path=path))
            self.assertEqual(
                "invalidated", replayed._state["runs"]["run-1"]["status"])
            self.assertEqual(
                "invalidated", replayed._state["claims"]["claim-1"]["status"])
            self.assertEqual(
                "invalidated", replayed._state["materials"]["mat-1"]["status"])
            self.assertIn("p2", replayed._state["withdrawn"])


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry()
        issue_happy_claim(self.reg)
        self.reg.receive_withdrawal(STEWARD, "p2")

    def test_reviewer_sees_full_lineage(self):
        view = LineageView(self.reg).for_claim(REVIEWER, "claim-1")
        self.assertEqual("cohort-1", view["cohort"]["cohort_id"])
        self.assertEqual("git:abc123", view["plan"]["code_version"])
        self.assertEqual("input-1", view["run"]["input_hash"])
        self.assertEqual("param-1", view["run"]["parameter_hash"])
        self.assertEqual("association", view["claim"]["claim_kind"])
        self.assertTrue(any(r["decision"] == "approved"
                            for r in view["claim"]["reviews"]))
        sources = view["invalidation_sources"]
        self.assertEqual("WITHDRAWAL_RECEIVED", sources[0]["source_event_type"])
        self.assertEqual("p2", sources[0]["participant_id"])
        self.assertEqual("mat-1", view["affected_materials"][0]["material_id"])
        self.assertEqual("invalidated", view["affected_materials"][0]["status"])

    def test_statistician_gets_minimal_redacted_view(self):
        view = LineageView(self.reg).for_claim(STAT, "claim-1")
        # 不暴露参与者标识
        self.assertNotIn("withdrawn_in_roster", view["cohort"])
        self.assertEqual("redacted", view["invalidation_sources"][0]["participant"])
        # 只给短指纹
        self.assertEqual("input-1", view["run"]["input_hash"][:7])
        self.assertNotIn("fingerprint", view["plan"]["variables"]["vars-1"])
        # 追溯链路关键节点仍齐全
        self.assertEqual("git:abc123", view["plan"]["code_version"])
        self.assertTrue(view["claim"]["reviews"])

    def test_unauthorized_actor_denied(self):
        with self.assertRaises(LineageDenied):
            LineageView(self.reg).for_claim(STAT2, "claim-1")


if __name__ == "__main__":
    unittest.main()
