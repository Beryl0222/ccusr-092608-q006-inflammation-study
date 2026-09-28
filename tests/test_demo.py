import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inflammation_study.demo import build_demo
from inflammation_study.registry import (
    DATA_STEWARD,
    PLATFORM_ADMIN,
    SCIENTIFIC_REVIEWER,
    Principal,
    Registry,
)
from inflammation_study.store import EventStore


class DemoScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "demo.jsonl"
        self.registry = Registry(EventStore(self.path))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_demo_invariants(self) -> None:
        summary = build_demo(self.registry)

        # 同回执重放沿用原事件
        self.assertEqual(summary["idempotent_rerun_event_id"], "evt-run-1")
        # 指纹冲突进入隔离区
        self.assertTrue(summary["quarantined_id"].startswith("quarantine:receipt-20260928-0001:"))
        # 撤回沿 run → claim×2(接受) + claim(退回也挂在该运行) → material 传播
        self.assertEqual(
            sorted(summary["withdrawal_invalidations"]),
            ["claim-crp-lvef", "claim-il6-causal", "claim-nlrp3-mech", "mat-faq-crp", "run-2026-0001"],
        )
        # 撤回幂等：重复登记只回传原事件
        self.assertEqual(summary["withdrawal_repeat_event_count"], 1)
        # 影像质控更正只打到引用旧指纹的老年分层链
        self.assertEqual(
            summary["qc_correction_invalidations"],
            [
                "run_record/run-2026-0002",
                "research_claim/claim-elderly-mri",
                "public_material/mat-slides-elderly",
            ],
        )
        # 所有被拒操作都有稳定错误码
        self.assertTrue(summary["rejected_actions"])
        self.assertTrue(all(r["code"] for r in summary["rejected_actions"]))

    def test_demo_stream_replays_from_disk(self) -> None:
        build_demo(self.registry)
        replayed = Registry(EventStore(self.path))
        admin = Principal("admin", PLATFORM_ADMIN)
        view = replayed.trace("mat-faq-crp", admin)
        self.assertEqual(view["status"], "invalidated")
        self.assertEqual(view["run"]["code_version"], "git:analysis-core@a91c2e7")
        self.assertEqual(view["run"]["cohort_id"], "cohort-2026-highinfl")
        self.assertEqual(view["claim"]["comment"], "关联方向与既往研究一致，已校正既定协变量。")
        # 受影响的后续材料可从运行侧列出
        run_view = replayed.trace("run-2026-0001", admin)
        self.assertEqual(
            {m["material_id"] for m in run_view["downstream_materials"]}, {"mat-faq-crp"}
        )

    def test_demo_role_views(self) -> None:
        build_demo(self.registry)
        public = self.registry.trace("mat-faq-crp", Principal("anon", "public"))
        steward = self.registry.trace("run-2026-0001", Principal("s", DATA_STEWARD))
        reviewer = self.registry.trace("mat-faq-crp", Principal("r", SCIENTIFIC_REVIEWER))
        self.assertNotIn("input_hash", public["run"])
        self.assertNotIn("reviewer_id", public["claim"])
        self.assertIn("sample_refs", steward["run"])
        self.assertIn("withdrawals", steward)
        self.assertIn("comment", reviewer["claim"])


if __name__ == "__main__":
    unittest.main()
