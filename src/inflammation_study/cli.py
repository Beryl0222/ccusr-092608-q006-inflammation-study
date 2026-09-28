"""命令行入口。

两种用法：

1. 领域事件契约校验（保持原有调用方式）：

   python -m inflammation_study.cli <schema.json> <event.json>

2. 从一条风险表述（已签发结论）追溯最小数据谱系：

   python -m inflammation_study.cli trace --store events.jsonl \\
       --claim claim-1 --as stat1:statistician
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .contracts import validate_event
from .events import EventStore
from .lineage import LineageView
from .registry import Registry, RegistryError

ROLE_ALIASES = {
    "admin": "platform_admin",
    "platform_admin": "platform_admin",
    "statistician": "statistician",
    "stat": "statistician",
    "steward": "data_steward",
    "data_steward": "data_steward",
    "reviewer": "scientific_reviewer",
    "scientific_reviewer": "scientific_reviewer",
}

DEFAULT_SCHEMA = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def _validate(schema_path: str, event_path: str) -> int:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def _parse_actor(spec: str) -> dict[str, str]:
    if ":" not in spec:
        raise SystemExit("--as 格式应为 subject_id:role")
    subject_id, role = spec.split(":", 1)
    role = ROLE_ALIASES.get(role, role)
    return {"subject_id": subject_id, "role": role}


def _trace(args: list[str]) -> int:
    store_path = claim_id = actor_spec = schema_path = None
    while args:
        flag = args.pop(0)
        if flag == "--store":
            store_path = args.pop(0)
        elif flag == "--claim":
            claim_id = args.pop(0)
        elif flag == "--as":
            actor_spec = args.pop(0)
        elif flag == "--schema":
            schema_path = args.pop(0)
        else:
            raise SystemExit(f"未知参数: {flag}")
    if not store_path or not claim_id or not actor_spec:
        raise SystemExit("trace 需要 --store、--claim、--as 参数")
    schema_file = Path(schema_path) if schema_path else DEFAULT_SCHEMA
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    store = EventStore(path=store_path, schema=schema)
    try:
        view = LineageView(Registry(store)).for_claim(_parse_actor(actor_spec), claim_id)
    except RegistryError as exc:
        print(f"拒绝追溯: {exc}", file=sys.stderr)
        return 1
    json.dump(view, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and not argv[0].startswith("-"):
        return _validate(argv[0], argv[1])
    if argv and argv[0] == "validate":
        if len(argv) != 3:
            print("用法: python -m inflammation_study.cli validate <schema.json> <event.json>",
                  file=sys.stderr)
            return 2
        return _validate(argv[1], argv[2])
    if argv and argv[0] == "trace":
        return _trace(argv[1:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
