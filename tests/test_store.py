import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inflammation_study.errors import ConflictError, ContractViolation
from inflammation_study.store import EventStore

EVENT = {
    "event_id": "evt-1",
    "event_type": "COHORT_FROZEN",
    "aggregate_type": "cohort_snapshot",
    "aggregate_id": "cohort-1",
    "occurred_at": "2026-09-28T10:00:00+08:00",
    "version": 1,
    "payload": {"label": "v1"},
}


class EventStoreTests(unittest.TestCase):
    def test_append_and_read(self) -> None:
        store = EventStore()
        stored = store.append(EVENT)
        self.assertEqual(stored["event_id"], "evt-1")
        self.assertEqual(store.events(), [EVENT])
        # 返回的是副本，改不到内部状态
        stored["payload"]["label"] = "tampered"
        self.assertEqual(store.events()[0]["payload"]["label"], "v1")

    def test_same_event_is_idempotent(self) -> None:
        store = EventStore()
        store.append(EVENT)
        again = store.append(dict(EVENT))
        self.assertEqual(again["event_id"], "evt-1")
        self.assertEqual(len(store.events()), 1)

    def test_same_id_different_body_conflicts(self) -> None:
        store = EventStore()
        store.append(EVENT)
        with self.assertRaises(ConflictError):
            store.append(dict(EVENT, payload={"label": "v2"}))
        # 冲突不写入
        self.assertEqual(len(store.events()), 1)

    def test_versions_mould_be_contiguous(self) -> None:
        store = EventStore()
        store.append(EVENT)
        with self.assertRaises(ContractViolation):
            store.append(dict(EVENT, event_id="evt-2", version=3))
        v2 = store.append(
            dict(EVENT, event_id="evt-2", version=2, occurred_at="2026-09-28T11:00:00+08:00")
        )
        self.assertEqual(v2["version"], 2)

    def test_bad_contract_rejected(self) -> None:
        store = EventStore()
        with self.assertRaises(ContractViolation):
            store.append(dict(EVENT, occurred_at="2026-09-28T10:00:00"))

    def test_jsonl_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            store.append(EVENT)
            store.append(
                dict(EVENT, event_id="evt-2", version=2, occurred_at="2026-09-28T11:00:00+08:00")
            )
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            replayed = EventStore(path)
            self.assertEqual(len(replayed.events()), 2)
            self.assertEqual(replayed.get("evt-2")["version"], 2)


if __name__ == "__main__":
    unittest.main()
