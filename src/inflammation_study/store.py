"""JSONL 追加式事件存储。

- 只追加，从不修改或删除历史事件（撤回与失效均以新事件表达）。
- event_id 全局唯一：完全相同的事件重复写入按幂等处理；同号不同内容判冲突。
- 每个聚合的 version 必须从 1 开始严格递增。
- 写入在进程内加线程锁；落盘时额外用 fcntl 互斥，避免多进程交叉写。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import validate_event
from .errors import ConflictError, ContractViolation

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


class EventStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._index: dict[str, dict[str, Any]] = {}
        self._versions: dict[tuple[str, str], int] = {}
        if self.path is not None and self.path.exists():
            self._replay_from_disk()

    # ------------------------------------------------------------------ 读

    def events(
        self,
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按追加顺序返回事件副本；不暴露内部可变状态。"""
        with self._lock:
            result = []
            for event in self._events:
                if aggregate_type is not None and event["aggregate_type"] != aggregate_type:
                    continue
                if aggregate_id is not None and event["aggregate_id"] != aggregate_id:
                    continue
                result.append(json.loads(json.dumps(event, ensure_ascii=False)))
            return result

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            event = self._index.get(event_id)
            return json.loads(json.dumps(event, ensure_ascii=False)) if event else None

    # ------------------------------------------------------------------ 写

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """校验并追加一个信封事件，返回已存储副本。"""
        issues = validate_event(event, load_schema())
        if issues:
            detail = "; ".join(f"{i.field}:{i.code}" for i in issues)
            raise ContractViolation(f"事件契约校验失败: {detail}")

        key = (event["aggregate_type"], event["aggregate_id"])
        stored = json.loads(json.dumps(event, ensure_ascii=False))
        with self._lock:
            existing = self._index.get(stored["event_id"])
            if existing is not None:
                if existing == stored:
                    return json.loads(json.dumps(existing, ensure_ascii=False))
                raise ConflictError("事件标识已存在但内容不一致", field="event_id")

            expected = self._versions.get(key, 0) + 1
            if stored["version"] != expected:
                raise ContractViolation(
                    f"聚合 {key[0]}/{key[1]} 版本号应为 {expected}，实际 {stored['version']}",
                    field="version",
                )

            if self.path is not None:
                self._persist(stored)
            self._events.append(stored)
            self._index[stored["event_id"]] = stored
            self._versions[key] = stored["version"]
            return json.loads(json.dumps(stored, ensure_ascii=False))

    def append_many(self, events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [self.append(event) for event in events]

    # ---------------------------------------------------------------- 内部

    def _persist(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False) + "\n"
        try:
            import fcntl

            with self.path.open("a", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.write(line)
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ModuleNotFoundError:  # 非 POSIX 平台退化为进程内锁
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _replay_from_disk(self) -> None:
        for line_no, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            event = json.loads(line)
            issues = validate_event(event, load_schema())
            if issues:
                raise ContractViolation(f"存储第 {line_no} 行事件无法通过契约校验")
            if event["event_id"] in self._index:
                raise ConflictError(f"存储第 {line_no} 行事件标识重复: {event['event_id']}")
            key = (event["aggregate_type"], event["aggregate_id"])
            expected = self._versions.get(key, 0) + 1
            if event["version"] != expected:
                raise ContractViolation(f"存储第 {line_no} 行版本号断裂")
            self._events.append(event)
            self._index[event["event_id"]] = event
            self._versions[key] = event["version"]
