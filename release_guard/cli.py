from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m release_guard",
        description=(
            "Generate three schema versions, normalize them, run the main "
            "progressive-release engine, and cross-check with an independent "
            "reference runner."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20261127,
        help="deterministic sample seed (default: 20261127)",
    )
    parser.add_argument(
        "--samples-dir",
        default="samples",
        help="directory for deterministic schema samples (default: samples)",
    )
    parser.add_argument(
        "--output-dir",
        default="artifacts",
        help="directory for normalized inputs and reports (default: artifacts)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only print the final status and report path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workdir = Path.cwd()
    summary = run_pipeline(
        workdir=workdir,
        seed=args.seed,
        samples_dir=Path(args.samples_dir),
        output_dir=Path(args.output_dir),
    )
    run = summary["summary"]
    if not args.quiet:
        print("逐步裁决:")
        for item in summary["review"]["step_decisions"]:
            trigger = item.get("trigger") or {}
            if item["verdict"] == "rollback":
                trigger_text = (
                    f" trigger={trigger.get('metric')} "
                    f"{trigger.get('observed')}>{trigger.get('threshold')} "
                    f"streak={trigger.get('consecutive_breaches')}"
                )
            elif item["pause_count"]:
                trigger_text = f" pauses={item['pause_count']} recovered"
            else:
                trigger_text = ""
            target = item.get("rollback_target")
            target_text = (
                f" target=step-{target['step_number']}" if target else ""
            )
            print(
                f"  step {item['step_number']} {item['traffic_percent']}% "
                f"{item['verdict']}{trigger_text}{target_text}"
            )

        rollback_actions = summary["review"]["rollback_actions"]
        print("回退动作:")
        if rollback_actions:
            for action in rollback_actions:
                print(
                    f"  {action['sequence']}. {action['action']}: "
                    f"{action['detail']}"
                )
        else:
            print("  none")

        print("放量账目:")
        for entry in summary["review"]["ledger"]:
            metrics = entry["metrics"]
            print(
                f"  step {entry['step_number']} {entry['declared_traffic_percent']}% "
                f"{entry['verdict']} actual={entry['actual_rollout_seconds']}s "
                f"paused={entry['paused_seconds']}s "
                f"pause={entry['pause_count']} rollback={entry['rollback_count']} "
                f"error={metrics['error_rate']['breach_count']}/"
                f"{metrics['error_rate']['spike_count']} "
                f"latency={metrics['latency_p95_ms']['breach_count']}/"
                f"{metrics['latency_p95_ms']['spike_count']} "
                f"drop={metrics['session_drop_rate']['breach_count']}/"
                f"{metrics['session_drop_rate']['spike_count']}"
            )
        print(
            "跨版本: "
            f"normalized={summary['cross_version']['normalized_core_equal']} "
            f"decisions={summary['cross_version']['decisions_equal']} "
            f"rollback={summary['cross_version']['rollback_targets_equal']} "
            f"ledger={summary['cross_version']['ledgers_equal']} "
            f"differences={summary['cross_version']['difference_field_count']}"
        )
        print(
            "独立参考推进器: "
            + ("全部一致" if summary["checks"]["reference_all_match"] else "不一致")
        )
        print(f"结果文件: {summary['reports']['markdown']}")
        print(f"验收文件: {summary['reports']['verification']}")
    else:
        print(f"结果文件: {summary['reports']['markdown']}")
    if summary["all_passed"]:
        print("STATUS: PASS")
        return 0
    print("STATUS: FAIL")
    verification = summary["checks"]
    print(f"检查明细: {verification}", file=sys.stderr)
    return 1

