from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .generator import generate_scenario
from .pipeline import run_pipeline
from .reporting import write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hotel_edge_kv",
        description=(
            "生成确定性客房服务请求流，执行边缘 KV 缓存批调度、取消回收核对、"
            "持久化续算与摘要输出。"
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "generate"),
        default="run",
        help="run: 生成并跑完整流水线；generate: 仅写出确定性输入",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20261128,
        help="确定性生成器种子（默认：20261128）",
    )
    parser.add_argument(
        "--scenario",
        choices=("mixed", "boundary"),
        default="mixed",
        help="mixed 为完整样例，boundary 为精简边界样例",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results"),
        help="结果目录（默认：results）",
    )
    parser.add_argument(
        "--resume-at",
        type=int,
        default=None,
        help="续算 checkpoint 的逻辑时刻（默认使用场景设置）",
    )
    return parser


def _print_summary(summary: dict) -> None:
    scheduler = summary["scheduler"]
    cancellation = summary["cancellation"]
    print("酒店边缘 KV 调度完成")
    print(
        f"  请求: {scheduler['requests']['total']}，"
        f"完成: {scheduler['requests']['completed']}，"
        f"取消: {scheduler['requests']['cancelled']}，"
        f"token: {scheduler['tokens_generated']}"
    )
    print(
        f"  取消回收: {cancellation['reclaimed_blocks']} 块 / "
        f"{cancellation['reclaimed_bytes']} 字节，"
        f"即时回收率: {cancellation['immediate_reclaim_rate']}"
    )
    print(
        f"  竞争裁决: {summary['competition']['count']}，"
        f"核对异常: {summary['audit']['issue_count']}"
    )
    print(
        f"  续算快照一致: {summary['resume']['snapshot_equal']}，"
        f"续算基准一致: {summary['resume']['matches_reference']}"
    )
    print(f"  总体验收: {summary['acceptance']['status']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenario = generate_scenario(seed=args.seed, scenario_name=args.scenario)

    if args.command == "generate":
        output = args.output_dir / "inputs" / "scenario.json"
        write_json(output, scenario.to_dict())
        print(json.dumps({"scenario_file": str(output)}, ensure_ascii=False))
        return 0

    summary = run_pipeline(
        scenario,
        output_dir=args.output_dir,
        resume_at=args.resume_at,
    )
    _print_summary(summary)
    print(f"结果目录: {args.output_dir}")
    return 0 if summary["acceptance"]["status"] == "ok" else 2
