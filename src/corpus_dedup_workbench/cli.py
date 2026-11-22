from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import WorkbenchConfig
from .errors import WorkbenchError
from .generation import (
    DEFAULT_DOCS_PER_SHARD,
    DEFAULT_SEED,
    DEFAULT_SHARDS,
    generate_sample,
)
from .service import DedupService

DEFAULT_DATA = "data/manifest.jsonl"
DEFAULT_CONFIG = "config/dedup_config.json"
DEFAULT_CACHE = ".cache/dedup"
DEFAULT_BATCH = "artifacts/batch_result.json"


def _print(value: Any, pretty: bool = True) -> None:
    if pretty:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _service(args: argparse.Namespace) -> DedupService:
    config = WorkbenchConfig.load(args.config)
    return DedupService(
        args.data,
        config,
        cache_root=args.cache_root,
    )


def _cmd_generate(args: argparse.Namespace) -> int:
    result = generate_sample(
        args.out,
        args.config,
        seed=args.seed,
        shards=args.shards,
        docs_per_shard=args.docs_per_shard,
    )
    _print(result, args.pretty)
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.batch()
    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.summary:
        _print(
            {
                "ok": True,
                "totals": result["totals"],
                "cache_report": result["cache_report"],
                "snapshots": result["snapshots"],
                "output": args.out,
            },
            args.pretty,
        )
    else:
        _print(result, args.pretty)
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.query(
        args.doc_id,
        latency_budget_ms=args.budget_ms,
    )
    _print(result, args.pretty)
    return 0


def _cmd_compare_modes(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.compare_modes()
    _print(result, args.pretty)
    return 0


def _cmd_consistency(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.validate_all_consistency()
    _print(result, args.pretty)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    service = _service(args)
    _print(service.status(), args.pretty)
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    service = _service(args)
    before = service.status()
    result = service.refresh()
    _print(
        {
            "ok": True,
            "before_snapshots": before["snapshots"],
            "after_snapshots": result["snapshots"],
            "cache_report": result["cache_report"],
        },
        args.pretty,
    )
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    service = _service(args)
    batch = service.batch()
    consistency = service.validate_all_consistency()
    refresh = service.refresh()
    first_doc_id = min(batch["docs"])
    query_result = service.query(first_doc_id)
    mode_diff = service.compare_modes()
    checks = {
        "batch_has_docs": batch["totals"]["doc_count"] > 0,
        "all_queries_consistent": consistency["all_consistent"],
        "query_within_budget": query_result["latency"]["within_budget"],
        "cache_refresh_reused_all": (
            len(refresh["cache_report"]["reused_docs"])
            == batch["totals"]["doc_count"]
        ),
        "snapshot_versions_monotonic": all(
            after["version"] >= before["version"]
            for (_, before), (_, after) in zip(
                batch["snapshots"].items(), refresh["snapshots"].items()
            )
        ),
        "mode_difference_observable": (
            len(mode_diff["removed_set_difference"]["only_full_mode_removed"]) > 0
            or len(mode_diff["hit_position_differences"]) > 0
        ),
    }
    report = {
        "ok": all(checks.values()),
        "checks": checks,
        "sample": {
            "doc_count": batch["totals"]["doc_count"],
            "candidate_pairs": batch["totals"]["candidate_pair_count"],
            "removed": batch["totals"]["removed_count"],
        },
        "consistency": consistency,
        "query": {
            "doc_id": query_result["doc_id"],
            "actual_ms": query_result["latency"]["actual_ms"],
            "budget_ms": query_result["latency"]["budget_ms"],
            "removed": query_result["removed"],
        },
        "mode_difference": {
            "only_full_mode_removed": mode_diff["removed_set_difference"][
                "only_full_mode_removed"
            ],
            "attribution_summary": mode_diff["attribution_summary"],
        },
        "snapshots": refresh["snapshots"],
        "cache_report": refresh["cache_report"],
    }
    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _print(report, args.pretty)
    return 0 if report["ok"] else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    from .web import run_server

    service = _service(args)
    run_server(service, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dedup-workbench",
        description="本地确定性语料去重诊断工作台",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="生成固定种子样本")
    generate.add_argument("--out", default="data")
    generate.add_argument("--config", default=DEFAULT_CONFIG)
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--shards", type=int, default=DEFAULT_SHARDS)
    generate.add_argument(
        "--docs-per-shard", type=int, default=DEFAULT_DOCS_PER_SHARD
    )
    generate.add_argument("--pretty", action="store_true", default=True)
    generate.set_defaults(func=_cmd_generate)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--data", default=DEFAULT_DATA)
        subparser.add_argument("--config", default=DEFAULT_CONFIG)
        subparser.add_argument("--cache-root", default=DEFAULT_CACHE)
        subparser.add_argument("--pretty", action="store_true", default=True)

    batch = subparsers.add_parser("batch", help="执行批量全量去重")
    common(batch)
    batch.add_argument("--out", default=DEFAULT_BATCH)
    batch.add_argument("--summary", action="store_true")
    batch.set_defaults(func=_cmd_batch)

    query = subparsers.add_parser("query", help="按 doc_id 查询")
    common(query)
    query.add_argument("--doc-id", required=True)
    query.add_argument("--budget-ms", type=float, default=None)
    query.set_defaults(func=_cmd_query)

    compare = subparsers.add_parser("compare-modes", help="比较全量与摘要模式")
    common(compare)
    compare.set_defaults(func=_cmd_compare_modes)

    consistency = subparsers.add_parser(
        "consistency", help="校验全部文档的查询/批量一致性"
    )
    common(consistency)
    consistency.set_defaults(func=_cmd_consistency)

    status = subparsers.add_parser("status", help="输出服务与快照状态")
    common(status)
    status.set_defaults(func=_cmd_status)

    refresh = subparsers.add_parser("refresh", help="同配置重跑以观察缓存复用")
    common(refresh)
    refresh.set_defaults(func=_cmd_refresh)

    selftest = subparsers.add_parser("selftest", help="执行端到端自检")
    common(selftest)
    selftest.add_argument("--out", default="artifacts/selftest_report.json")
    selftest.set_defaults(func=_cmd_selftest)

    serve = subparsers.add_parser("serve", help="启动本地可视化工作台")
    common(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except WorkbenchError as exc:
        _print(exc.to_dict())
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

