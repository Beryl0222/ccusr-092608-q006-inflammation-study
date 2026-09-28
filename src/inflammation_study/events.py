"""追加写事件存储：进程内有序日志，可选 JSONL 持久化。

存储只负责信封约束、event_id 幂等/冲突、聚合版本单调递增；
业务状态推导全部在上层服务的 fold 中完成，重放日志不再触发副作用。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractIssue, validate_event


class EventConflict(Exception):
    """同一 event_id 提交了不同内容。"""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"事件标识冲突，已隔离: {event_id}")
        self.event_id = event_id


class ContractViolation(Exception):
    """事件信封或载荷违反领域契约。"""

    def __init__(self, issues: Sequence[ContractIssue]) -> None:
        super().__init__("; ".join(f"{i.field} {i.code}" for i in issues))
        self.issues = list(issues)


class EventStore:
    def __init__(self, path: str | Path | None = None, schema: Mapping[str, Any] | None = None) -> None:
        self._path = Path(path) if path else None
        self._schema = schema
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._versions: dict[tuple[str, str], int] = {}
        if self._path and self._path.exists():
            self._replay()

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def _replay(self) -> None:
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            self._ingest(event, validate=False)

    def _ingest(self, event: Mapping[str, Any], *, validate: bool) -> dict[str, Any] | None:
        """在持锁状态下写入一条事件；幂等重复返回 None。"""
        event = dict(event)
        event_id = event.get("event_id")
        existing = self._by_id.get(event_id) if event_id else None
        if existing is not None:
            if existing == event:
                return None
            raise EventConflict(event_id)
        key = (event["aggregate_type"], event["aggregate_id"])
        expected = self._versions.get(key, 0) + 1
        if "version" not in event:
            event["version"] = expected
        if validate and self._schema is not None:
            issues = validate_event(event, self._schema)
            if issues:
                raise ContractViolation(issues)
        if event["version"] != expected:
            raise ContractViolation([
                ContractIssue("version", "version_conflict",
                              f"聚合 {key} 版本必须为 {expected}，收到 {event['version']}")
            ])
        self._versions[key] = event["version"]
        self._events.append(event)
        self._by_id[event["event_id"]] = event
        return event

    def append_many(self, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """在同一把锁内追加一批事件，保证级联事件的顺序与版本连续。"""
        appended: list[dict[str, Any]] = []
        with self._lock:
            for event in events:
                stored = self._ingest(event, validate=True)
                if stored is not None:
                    appended.append(stored)
            if self._path is not None and appended:
                with self._path.open("a", encoding="utf-8") as handle:
                    for event in appended:
                        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return appended

    def append(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        result = self.append_many([event])
        return result[0] if result else None
