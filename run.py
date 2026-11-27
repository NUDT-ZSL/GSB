#!/usr/bin/env python3
"""Entry point for the deterministic port B+Tree index laboratory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from port_index_lab.runner import LabConfig, run_lab


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate deterministic port records, build observable B+Tree "
            "indexes, verify range scans against full-table baselines, and "
            "persist JSON snapshots/reports."
        )
    )
    parser.add_argument("--seed", type=int, default=20260701, help="fixed RNG seed")
    parser.add_argument(
        "--batches",
        type=int,
        default=4,
        help="number of write batches (>= 2)",
    )
    parser.add_argument(
        "--page-capacity",
        type=int,
        default=12,
        help="maximum keys per B+Tree page (>= 3)",
    )
    parser.add_argument(
        "--fill-factor",
        type=float,
        default=0.5,
        help="left-page key fraction used for every split",
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=10,
        help="number of generated queries before the final empty query",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="directory for JSON snapshots and reports",
    )
    parser.add_argument(
        "--stop-after-batch",
        type=int,
        default=None,
        help="write snapshots only through this batch, for interruption drills",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume each index from its latest batch snapshot",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only print the final JSON path and acceptance status",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = LabConfig(
        seed=args.seed,
        batches=args.batches,
        page_capacity=args.page_capacity,
        fill_factor=args.fill_factor,
        query_count=args.queries,
        output_dir=args.output_dir,
        stop_after_batch=args.stop_after_batch,
        resume=args.resume,
    )
    try:
        report = run_lab(config)
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    status = report.get("status", "partial")
    report_path = config.output_dir / "reports" / (
        "partial_report.json" if status == "partial" else "final_report.json"
    )
    if not args.quiet:
        print("港口 B+Tree 索引实验")
        print(f"- 状态: {status}")
        print(f"- 报告: {report_path}")
        if status == "partial":
            print("- 已按要求停在指定批次；追加 --resume 可继续。")
        else:
            choice = report["index_comparison"]["choice"]
            print(
                "- 泊位重叠查询首选索引: "
                f"{choice['primary_for_berth_overlap']} "
                f"({choice['primary_label']})"
            )
            print(
                "- 全局靠泊时刻闭区间建议二级索引: "
                f"{choice['secondary_for_global_berth_time']} "
                f"({choice['secondary_label']})"
            )
            print(f"- 验收: {'通过' if report['acceptance_passed'] else '失败'}")
            print("- 查询与全表基准: ", end="")
            if report["acceptance_checks"]["all_queries_match_full_table_baseline"]:
                print("全部一致")
            else:
                print("存在不一致，详见 acceptance_report.json")
            print(
                "- 增量/重建与快照恢复/连续写入: ",
                end="",
            )
            if (
                report["acceptance_checks"][
                    "all_incremental_queries_match_full_rebuild"
                ]
                and report["acceptance_checks"][
                    "all_recovery_queries_match_continuous"
                ]
            ):
                print("全部一致")
            else:
                print("存在不一致，详见路径核对字段")
    return 0 if status in {"passed", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
