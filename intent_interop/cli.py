"""Command-line interface for the intent interoperability demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .generator import generate_fixtures
from .injection import FaultInjector, parse_injection_specs
from .io_utils import write_json_atomic
from .matching import ResourceLimits
from .pipeline import (
    adjudicate,
    injection_run,
    match_single,
    migration_report,
    snapshot_scenario,
)


def _limits(args: argparse.Namespace) -> ResourceLimits:
    return ResourceLimits(
        max_keywords=args.max_keywords,
        max_query_codepoints=args.max_query_codepoints,
        max_match_entries=args.max_match_entries,
        policy=args.resource_policy,
    )


def _emit(report: dict[str, Any], report_path: str | Path, exit_code: int) -> int:
    write_json_atomic(report_path, report)
    summary = {
        "status": report.get("status"),
        "code": report.get("code"),
        "report": str(report_path),
    }
    if "queries" in report:
        summary["query_count"] = len(report["queries"])
    if "match" in report:
        summary["result_count"] = len(report["match"].get("results", []))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code


def _add_resource_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-keywords", type=int, default=64)
    parser.add_argument("--max-query-codepoints", type=int, default=256)
    parser.add_argument("--max-match-entries", type=int, default=4096)
    parser.add_argument(
        "--resource-policy",
        choices=("error", "prefix"),
        default="error",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intent-interop",
        description="中文意图配置跨版本互操作与确定性判定示例",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="生成确定性样本与配置")
    generate.add_argument("--out-dir", default="data")

    baseline = subparsers.add_parser(
        "baseline",
        help="三格式加载/迁移/判定一致性裁决",
    )
    baseline.add_argument("--samples", required=True)
    baseline.add_argument("--v1", required=True)
    baseline.add_argument("--v2", required=True)
    baseline.add_argument("--v3", required=True)
    baseline.add_argument("--report", required=True)
    baseline.add_argument("--retired-policy", choices=("fallback", "reject"), default="fallback")
    _add_resource_args(baseline)

    match = subparsers.add_parser("match", help="按单份配置执行确定性判定")
    match.add_argument("--samples", required=True)
    match.add_argument("--config", required=True)
    match.add_argument("--report", required=True)
    match.add_argument("--retired-policy", choices=("fallback", "reject"), default="fallback")
    _add_resource_args(match)

    migrate = subparsers.add_parser("migrate", help="执行显式版本迁移")
    migrate.add_argument("--config", required=True)
    migrate.add_argument("--from-version", choices=("v1", "v2", "v3"))
    migrate.add_argument("--to", dest="target_version", choices=("v1", "v2", "v3"), required=True)
    migrate.add_argument("--out", required=True)
    migrate.add_argument("--report", required=True)
    migrate.add_argument("--samples")
    migrate.add_argument(
        "--hierarchy-policy",
        choices=("drop", "flatten", "reject"),
        default="drop",
    )
    migrate.add_argument("--loss-policy", choices=("allow", "reject"), default="allow")

    snapshot = subparsers.add_parser(
        "snapshot",
        help="加载初始/替换快照并验证整体替换与回退保护",
    )
    snapshot.add_argument("--samples", required=True)
    snapshot.add_argument("--initial", required=True)
    snapshot.add_argument("--replacement", required=True)
    snapshot.add_argument("--report", required=True)
    snapshot.add_argument("--retired-policy", choices=("fallback", "reject"), default="fallback")
    _add_resource_args(snapshot)

    inject = subparsers.add_parser("inject", help="执行读取/迁移/判定阶段异常注入")
    inject.add_argument("--samples", required=True)
    inject.add_argument("--config", required=True)
    inject.add_argument("--inject", action="append", required=True)
    inject.add_argument("--report", required=True)
    inject.add_argument("--retired-policy", choices=("fallback", "reject"), default="fallback")
    _add_resource_args(inject)

    demo = subparsers.add_parser("demo", help="生成数据并执行全部验收场景")
    demo.add_argument("--data-dir", default="data")
    demo.add_argument("--report-dir", default="reports")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            manifest = generate_fixtures(args.out_dir)
            print(
                json.dumps(
                    {
                        "status": "OK",
                        "out_dir": args.out_dir,
                        "query_count": manifest["query_count"],
                        "file_count": len(manifest["files"]),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "baseline":
            report, code = adjudicate(
                samples_path=args.samples,
                v1_path=args.v1,
                v2_path=args.v2,
                v3_path=args.v3,
                limits=_limits(args),
                retired_policy=args.retired_policy,
            )
            return _emit(report, args.report, code)
        if args.command == "match":
            report, code = match_single(
                samples_path=args.samples,
                config_path=args.config,
                limits=_limits(args),
                retired_policy=args.retired_policy,
            )
            return _emit(report, args.report, code)
        if args.command == "migrate":
            report, code = migration_report(
                config_path=args.config,
                target_version=args.target_version,
                out_path=args.out,
                from_version=args.from_version,
                hierarchy_policy=args.hierarchy_policy,
                loss_policy=args.loss_policy,
                samples_path=args.samples,
            )
            return _emit(report, args.report, code)
        if args.command == "snapshot":
            report, code = snapshot_scenario(
                samples_path=args.samples,
                initial_path=args.initial,
                replacement_path=args.replacement,
                retired_policy=args.retired_policy,
                limits=_limits(args),
            )
            return _emit(report, args.report, code)
        if args.command == "inject":
            specs = parse_injection_specs(args.inject)
            report, code = injection_run(
                samples_path=args.samples,
                config_path=args.config,
                injector=FaultInjector(specs),
                limits=_limits(args),
                retired_policy=args.retired_policy,
            )
            return _emit(report, args.report, code)
        if args.command == "demo":
            from .demo import run_demo

            summary = run_demo(args.data_dir, args.report_dir)
            print(
                json.dumps(
                    {
                        "status": summary["status"],
                        "passed": summary["passed"],
                        "failed": summary["failed"],
                        "report": str(Path(args.report_dir) / "demo_summary.json"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if summary["status"] == "OK" else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "E_UNATTRIBUTED",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    return 1
