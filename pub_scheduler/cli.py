"""Command-line demo for the publication pressure scheduler."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Any

from .audit import verify_result
from .checkpoint import CheckpointError, load_checkpoint, save_checkpoint
from .engine import SchedulerEngine
from .generator import generate_workload
from .model import ValidationError, dump_workload, load_workload

DEFAULT_SEED = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "数字出版平台多维资源争用裁决器：确定性生成、降级回写、"
            "失守追溯与检查点恢复。"
        )
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"确定性样本种子（默认 {DEFAULT_SEED}）",
    )
    parser.add_argument(
        "--load-workload",
        metavar="PATH",
        help="加载已有作业工程 JSON，而不是按种子生成",
    )
    parser.add_argument(
        "--dump-workload",
        metavar="PATH",
        help="将本次作业工程按规范顺序写入 JSON",
    )
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="只生成作业工程，不推进裁决",
    )
    parser.add_argument(
        "--checkpoint-at",
        type=int,
        metavar="TIME",
        help="推进到指定逻辑时刻后写入检查点",
    )
    parser.add_argument(
        "--checkpoint-out",
        metavar="PATH",
        default=None,
        help="检查点输出路径（默认 artifacts/checkpoint.json）",
    )
    parser.add_argument(
        "--stop-at-checkpoint",
        action="store_true",
        help="写入检查点后停止，不继续跑完",
    )
    parser.add_argument(
        "--resume",
        metavar="PATH",
        help="从带内容摘要校验的检查点恢复并继续",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="向 stdout 输出完整 JSON 结果",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="额外将完整 JSON 结果写入文件",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过独立日志重放审计",
    )
    return parser


def _print_human(result: dict[str, Any]) -> None:
    print("数字出版平台资源争用裁决复盘")
    print(
        f"seed={result.get('seed')} workload_hash={result['workload_hash']} "
        f"final_time={result['times']['current']}"
    )
    print(
        f"事件位置={result['times']['event_index']}/{result['times']['total_events']} "
        f"决策={len(result['decisions'])} 容量变更={len(result['capacity_changes'])}"
    )

    decisions_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for decision in result["decisions"]:
        decisions_by_time[decision["at"]].append(decision)
    changes_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for change in result["capacity_changes"]:
        changes_by_time[change["at"]].append(change)

    for snapshot in result["samples"]:
        at = snapshot["at"]
        print(f"\n[逻辑时刻 {at}]")
        time_decisions = decisions_by_time.get(at, [])
        if time_decisions:
            print("  准入决策清单:")
            for decision in time_decisions:
                node = decision.get("node_id") or "-"
                action = decision.get("action", "")
                action_text = f" {action}" if action else ""
                print(
                    f"    {decision['decision_id']} {decision['type']}{action_text} "
                    f"{decision['job_id']} @ {node} "
                    f"[{decision['reason']}] {decision['reason_text']}"
                )
        else:
            print("  准入决策清单: 无（仅生命周期容量变更）")

        time_changes = changes_by_time.get(at, [])
        if time_changes:
            print("  容量变更:")
            for change in time_changes:
                delta = change["delta"]
                cause_id = change["cause"]["id"]
                print(
                    f"    {change['change_id']} {change['node_id']} {change['job_id']} "
                    f"{change['operation']} delta=({delta['cpu']},"
                    f"{delta['memory']},{delta['io']}) cause={cause_id}"
                )

        print("  节点余量:")
        for node in snapshot["nodes"]:
            capacity = node["capacity"]
            free = node["free"]
            used = node["used"]
            print(
                f"    {node['node_id']}: free CPU={free['cpu']}/{capacity['cpu']} "
                f"MEM={free['memory']}/{capacity['memory']} "
                f"IO={free['io']}/{capacity['io']} "
                f"used=({used['cpu']},{used['memory']},{used['io']}) "
                f"active={','.join(node['active_jobs']) or '-'}"
            )

    print("\n紧迫度排序依据表:")
    print("  time job state priority remaining score sort_key decision")
    for row in result["urgency_table"]:
        print(
            f"  {row['at']:>4} {row['job_id']} {row['state']:<7} "
            f"p={row['priority']} remain={row['remaining']:>4} "
            f"score={row['urgency_score']:>7} "
            f"key={json.dumps(row['sort_key'], ensure_ascii=False)} "
            f"{row['decision_id']}"
        )

    print("\n业务类别资源满足率与截止达成:")
    print(
        "  category jobs admit complete preempt reject cancel downgrade "
        "resource_sat deadline_met deadline_miss"
    )
    for category, bucket in result["summary"]["by_category"].items():
        print(
            f"  {category} {bucket['jobs_submitted']} {bucket['jobs_admitted']} "
            f"{bucket['jobs_completed']} {bucket['jobs_preempted']} "
            f"{bucket['jobs_rejected']} {bucket['jobs_canceled']} "
            f"{bucket['jobs_downgraded']} {bucket['resource_satisfaction_ratio']:.4f} "
            f"{bucket['deadline_met']} {bucket['deadline_missed']}"
        )

    misses = result["summary"]["deadline_misses"]
    print(f"\n业务截止失守追溯 ({len(misses)} 项):")
    if not misses:
        print("  无")
    for miss in misses:
        print(
            f"  {miss['job_id']} {miss['category']} "
            f"deadline={miss['deadline']} status={miss['status']} "
            f"reason={miss['miss_reason']}"
        )
        print(
            f"    chain={','.join(miss['chain_decision_ids']) or '-'} "
            f"competitors={','.join(miss['competition_job_ids']) or '-'}"
        )

    verification = result.get("verification")
    if verification is not None:
        status = "通过" if verification["ok"] else "失败"
        print(f"\n独立账本重放审计: {status}")
        for error in verification["errors"]:
            print(f"  - {error}")


def _make_engine(args: argparse.Namespace) -> tuple[SchedulerEngine, dict[str, Any]]:
    metadata: dict[str, Any] = {}
    if args.resume:
        if args.load_workload:
            raise ValueError("--resume 与 --load-workload 不能同时使用")
        engine = load_checkpoint(args.resume)
        metadata["resumed_from"] = getattr(engine, "checkpoint_metadata", {})
    else:
        workload = (
            load_workload(args.load_workload)
            if args.load_workload
            else generate_workload(args.seed)
        )
        engine = SchedulerEngine(workload)
    return engine, metadata


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.stop_at_checkpoint and args.checkpoint_at is None:
        parser.error("--stop-at-checkpoint 必须与 --checkpoint-at 同时使用")

    try:
        engine, metadata = _make_engine(args)
        if args.dump_workload:
            dump_workload(engine.workload, args.dump_workload)
        if args.generate_only:
            output = json.dumps(
                engine.workload, ensure_ascii=False, indent=2
            )
            if args.output:
                directory = os.path.dirname(os.path.abspath(args.output))
                os.makedirs(directory, exist_ok=True)
                with open(args.output, "w", encoding="utf-8") as handle:
                    handle.write(output)
                    handle.write("\n")
            print(output)
            return 0

        if args.checkpoint_at is not None:
            engine.run_until(args.checkpoint_at)
            checkpoint_path = args.checkpoint_out or "artifacts/checkpoint.json"
            envelope = save_checkpoint(engine, checkpoint_path)
            metadata["checkpoint_written"] = {
                "path": os.path.abspath(checkpoint_path),
                "at": engine.current_time,
                "checkpoint_version": envelope["checkpoint_version"],
                "content_sha256": envelope["content_sha256"],
            }
            if not args.stop_at_checkpoint:
                engine.run_to_end()
        elif not engine.finished:
            engine.run_to_end()

        result = engine.result()
        if metadata:
            result["checkpoint"] = metadata
        if not args.no_verify:
            result["verification"] = verify_result(engine.workload, result)

        json_text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            directory = os.path.dirname(os.path.abspath(args.output))
            os.makedirs(directory, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(json_text)
                handle.write("\n")
        if args.json:
            print(json_text)
        else:
            _print_human(result)
            if args.output:
                print(f"\n完整 JSON 已写入: {os.path.abspath(args.output)}")

        verification = result.get("verification")
        if verification is not None and not verification["ok"]:
            return 1
        errors = result.get("invariant_errors", [])
        if errors:
            for error in errors:
                print(f"不变量错误: {error}", file=sys.stderr)
            return 1
        return 0
    except (CheckpointError, ValidationError, ValueError, OSError) as error:
        print(f"错误: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

