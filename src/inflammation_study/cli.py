"""命令行入口。

用法：
  python -m inflammation_study.cli validate <schema.json> <event.json>
      旧用法：校验单个事件信封。
  python -m inflammation_study.cli demo <store.jsonl>
      生成覆盖全流程的演示事件流（冻结→登记→名额→运行→隔离→
      签发→审阅→公开→撤回级联），并打印要点。
  python -m inflammation_study.cli find <store.jsonl> <风险表述关键词>
      按表述文本检索结论标识。
  python -m inflammation_study.cli trace <store.jsonl> <目标标识>
      --role admin|statistician|steward|reviewer|public
      从公开材料/结论/运行追溯最小数据谱系，按角色裁剪字段。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import RegistryError
from .registry import (
    DATA_STEWARD,
    PLATFORM_ADMIN,
    SCIENTIFIC_REVIEWER,
    STATISTICIAN,
    Principal,
    Registry,
)
from .store import EventStore, load_schema

_ROLE_ALIASES = {
    "admin": PLATFORM_ADMIN,
    "platform_admin": PLATFORM_ADMIN,
    "statistician": STATISTICIAN,
    "stat": STATISTICIAN,
    "steward": DATA_STEWARD,
    "data_steward": DATA_STEWARD,
    "reviewer": SCIENTIFIC_REVIEWER,
    "scientific_reviewer": SCIENTIFIC_REVIEWER,
    "public": "public",
}


def _cmd_validate(args: argparse.Namespace) -> int:
    from .contracts import validate_event

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}\t{issue.code}\t{issue.message}")
    return 1


def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import build_demo

    store = EventStore(args.store)
    summary = build_demo(Registry(store))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n事件流已写入: {args.store}", file=sys.stderr)
    return 0


def _cmd_find(args: argparse.Namespace) -> int:
    registry = Registry(EventStore(args.store))
    matches = registry.find_claims_by_text(args.query)
    if not matches:
        print("无匹配结论", file=sys.stderr)
        return 1
    for claim_id in matches:
        print(claim_id)
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    role = _ROLE_ALIASES.get(args.role)
    if role is None:
        print(f"未知角色: {args.role}", file=sys.stderr)
        return 2
    registry = Registry(EventStore(args.store))
    actor = Principal(args.as_principal, role)
    view = registry.trace(args.target, actor)
    print(json.dumps(view, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inflammation_study", description="衍生分析登记服务")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验单个事件信封")
    p_validate.add_argument("schema")
    p_validate.add_argument("event")
    p_validate.set_defaults(func=_cmd_validate)

    p_demo = sub.add_parser("demo", help="生成演示事件流")
    p_demo.add_argument("store")
    p_demo.set_defaults(func=_cmd_demo)

    p_find = sub.add_parser("find", help="按风险表述检索结论")
    p_find.add_argument("store")
    p_find.add_argument("query")
    p_find.set_defaults(func=_cmd_find)

    p_trace = sub.add_parser("trace", help="追溯最小数据谱系")
    p_trace.add_argument("store")
    p_trace.add_argument("target", help="公开材料、结论或运行标识")
    p_trace.add_argument(
        "--role",
        choices=sorted(set(_ROLE_ALIASES)),
        default="admin",
        help="查看身份（默认 admin）",
    )
    p_trace.add_argument("--as", dest="as_principal", default="cli-user", help="查看者标识")
    p_trace.set_defaults(func=_cmd_trace)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RegistryError as exc:
        print(f"{exc.field}\t{exc.code}\t{exc.message}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"$\tfile_not_found\t{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
