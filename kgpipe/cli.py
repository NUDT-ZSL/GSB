from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .demo import run_demo
from .generate import generate_sample_data
from .pipeline import run_pipeline
from .snapshot import SnapshotStore
from .util import pretty_json, read_json
from .verifier import verify_execution_report


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "pair_budget": args.pair_budget,
        "memory_limit_mb": args.memory_limit_mb,
        "reserved_memory_mb": args.reserved_memory_mb,
        "approx_neighbors": args.approx_neighbors,
        "budget_policy": args.budget_policy,
        "config_version": args.config_version,
    }


def _command_generate(args: argparse.Namespace) -> int:
    metadata = generate_sample_data(args.out, seed=args.seed)
    print(pretty_json(metadata), end="")
    return 0


def _command_run(args: argparse.Namespace) -> int:
    report = run_pipeline(
        data_dir=args.data,
        out_dir=args.out,
        config_path=args.config,
        overrides=_overrides(args),
        requested_mode=args.mode,
        snapshot_dir=args.snapshot_dir,
        graph_version=args.graph_version,
        self_check=not args.no_self_check,
    )
    summary = report.get("summary") or {
        "status": report["status"],
        "errors": report.get("errors", []),
        "report": str(Path(args.out) / "report.json"),
    }
    print(pretty_json(summary), end="")
    return 0 if report["status"] in {"ok", "degraded"} else 3


def _command_validate(args: argparse.Namespace) -> int:
    try:
        report = read_json(args.report)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            pretty_json(
                {
                    "valid": False,
                    "error": f"无法读取报告: {exc}",
                }
            ),
            end="",
        )
        return 4
    verification = verify_execution_report(report, args.data)
    if args.out:
        from .util import atomic_write_json

        atomic_write_json(args.out, verification)
    print(pretty_json(verification), end="")
    return 0 if verification["valid"] else 4


def _command_snapshot_verify(args: argparse.Namespace) -> int:
    report = SnapshotStore(args.snapshot_dir).verify_all()
    if args.out:
        from .util import atomic_write_json

        atomic_write_json(args.out, report)
    print(pretty_json(report), end="")
    return 0 if report["valid"] else 5


def _command_demo(args: argparse.Namespace) -> int:
    summary = run_demo(args.out, seed=args.seed)
    print(pretty_json(summary), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kgpipe",
        description="本地确定性中文知识图谱构建、取舍裁决与结果验证工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate-data",
        help="按固定种子生成确定性语料、词典、模式表与真值",
    )
    generate.add_argument("--out", default="data", help="输出目录")
    generate.add_argument("--seed", type=int, default=20261121, help="固定随机种子")
    generate.set_defaults(handler=_command_generate)

    run = subparsers.add_parser("run", help="执行一次知识图谱构建管线")
    run.add_argument("--data", default="data", help="数据目录")
    run.add_argument("--out", default="artifacts/run", help="输出目录")
    run.add_argument("--config", default=None, help="抽取配置 JSON")
    run.add_argument(
        "--mode",
        choices=("auto", "exact", "approx", "both"),
        default="auto",
        help="路径模式；both 同时运行精确与近似路径",
    )
    run.add_argument("--pair-budget", type=int, default=None, help="总比较预算")
    run.add_argument(
        "--memory-limit-mb", type=int, default=None, help="驻留内存预算（MiB）"
    )
    run.add_argument(
        "--reserved-memory-mb", type=int, default=None, help="预留内存（MiB）"
    )
    run.add_argument(
        "--approx-neighbors",
        type=int,
        default=None,
        help="近似路径每头实体邻域尾实体上限",
    )
    run.add_argument(
        "--budget-policy",
        choices=("error", "degrade"),
        default=None,
        help="超预算时返回错误或降级到近似路径",
    )
    run.add_argument("--config-version", type=int, default=None)
    run.add_argument("--graph-version", type=int, default=None)
    run.add_argument("--snapshot-dir", default=None)
    run.add_argument("--no-self-check", action="store_true")
    run.set_defaults(handler=_command_run)

    validate = subparsers.add_parser(
        "validate",
        help="从源数据独立复核已有运行报告",
    )
    validate.add_argument("--data", default="data")
    validate.add_argument("--report", required=True)
    validate.add_argument("--out", default=None)
    validate.set_defaults(handler=_command_validate)

    snapshot_verify = subparsers.add_parser(
        "snapshot-verify",
        help="校验快照清单、版本与内容校验和",
    )
    snapshot_verify.add_argument("--snapshot-dir", required=True)
    snapshot_verify.add_argument("--out", default=None)
    snapshot_verify.set_defaults(handler=_command_snapshot_verify)

    demo = subparsers.add_parser(
        "demo",
        help="执行基线、跨路径、超预算、异常分片与快照损坏演示",
    )
    demo.add_argument("--out", default="artifacts/demo")
    demo.add_argument("--seed", type=int, default=20261121)
    demo.set_defaults(handler=_command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            pretty_json(
                {
                    "status": "fatal",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
            end="",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

