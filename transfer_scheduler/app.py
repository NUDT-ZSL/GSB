"""可交互命令行应用。

交互模式（无参数启动）展示菜单；非交互模式供脚本/验收使用：

``python -m transfer_scheduler.app demo-budget``
``python -m transfer_scheduler.app demo-failure``
``python -m transfer_scheduler.app demo-threshold``
``python -m transfer_scheduler.app run``
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .data import read_catalog
from .demo import (
    default_config,
    demo_budget_conflict,
    demo_threshold_stability,
    demo_worker_failure,
    prepare_workspace,
)
from .errors import BusinessError
from .service import ExecutionSummary, WorkflowService


def _print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _print_tasks(tasks: list) -> None:
    _print_header("任务清单（JSONL 字段全量）")
    header = (
        f"{'task_id':<10}{'src':<10}{'tgt':<10}{'adapter':<13}"
        f"{'dim':>4}{'samples':>9}{'strength':>10}{'prio':>5}{'seed':>14}"
    )
    print(header)
    for task in tasks:
        print(
            f"{task.task_id:<10}{task.source_domain:<10}{task.target_domain:<10}"
            f"{task.adapter_kind:<13}{task.input_dim:>4}{task.sample_count:>9}"
            f"{task.transfer_strength:>10.6f}{task.priority:>5}{task.seed:>14}"
        )


def _print_summary(summary: ExecutionSummary) -> None:
    _print_header(f"执行摘要 {summary.run_id}（快照版本 {summary.versions}）")
    print("准入集合:", list(summary.admitted))
    print("并行分组:")
    for index, group in enumerate(summary.groups):
        print(f"  wave {index}: {list(group)}")
    if summary.rejected:
        print("拒绝/降级裁决:")
        for item in summary.rejected:
            print(
                f"  {item['task_id']} -> {item['code']} "
                f"冲突={item['conflict_type']} rank={item['arbitration_rank']}"
            )
            print(f"    依据: {item['reason']}")
    else:
        print("拒绝/降级裁决: 无")
    print("每任务评估分数与门限:")
    for task_id, score in summary.scores.items():
        gate = summary.gate_decisions[task_id]
        flag = "ACCEPT" if gate["accepted"] else "REJECT"
        print(
            f"  {task_id}: score={score:.12f} 门限={flag} "
            f"tie_band={gate['in_tie_band']} ({gate['reason']})"
        )
    if summary.unavailable:
        print("不可用任务（worker 异常回传）:")
        for task_id, item in summary.unavailable.items():
            print(f"  {task_id}: {item['code']} @ {item['stage']} - {item['message']}")
    ledger = summary.ledger
    print(
        "预算账本: compute",
        f"{ledger['compute_used']}/{ledger['compute_budget']}",
        "memory",
        f"{ledger['memory_reserved']}/{ledger['memory_budget_bytes']}B",
        "slots",
        f"{ledger['slots_held']}/{ledger['parallel_slots']}",
    )
    print("一致性自检:", summary.evidence)
    if summary.last_errors:
        print("最近业务错误码:")
        for item in summary.last_errors:
            print(f"  {item['code']}: {item['message']}")
    else:
        print("最近业务错误码: 无")


def _print_threshold(decisions: dict) -> None:
    _print_header("门限稳定性保护演示（50 次重复裁决）")
    for strategy in ("strict", "band"):
        print(f"策略 {strategy}:")
        for item in decisions[strategy]:
            print(f"  value={item['value']:<12} accepted={item['accepted']}  {item['reason']}")
    print("重复运行稳定:", decisions["repeat_stable"])


def _workspace(base: Path) -> tuple[Path, list]:
    catalog = base / "var" / "data" / "tasks.jsonl"
    if catalog.exists():
        return catalog, read_catalog(catalog)
    return prepare_workspace(base)


def run_interactive(base: Path) -> None:
    catalog, tasks = _workspace(base)
    snapshot_dir = base / "var" / "snapshots"
    menu = (
        "\n可用操作:\n"
        "  1. 查看任务清单\n"
        "  2. 常规调度运行（含账本/快照/自检）\n"
        "  3. 演示：预算冲突拒绝\n"
        "  4. 演示：并行异常回传\n"
        "  5. 演示：门限稳定性\n"
        "  6. 重启加载快照并显示版本\n"
        "  7. 退出\n"
    )
    while True:
        print(menu)
        choice = input("请选择 [1-7]: ").strip()
        try:
            if choice == "1":
                _print_tasks(tasks)
            elif choice == "2":
                service = WorkflowService(default_config(), snapshot_dir)
                _print_summary(service.run(tasks, run_id="interactive-run", in_process=True))
            elif choice == "3":
                _print_summary(demo_budget_conflict(tasks, snapshot_dir / "budget-demo"))
            elif choice == "4":
                _print_summary(
                    demo_worker_failure(tasks, snapshot_dir / "failure-demo", in_process=True)
                )
            elif choice == "5":
                _print_threshold(demo_threshold_stability())
            elif choice == "6":
                service = WorkflowService(default_config(), snapshot_dir)
                print("恢复版本:", service.restore_from_snapshots())
            elif choice == "7":
                print("再见。")
                return
            else:
                print("无效选择，请重新输入。")
        except BusinessError as exc:
            print(f"[业务错误] {exc.code}: {exc.message} 上下文={exc.context}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨任务迁移适配调度组件")
    parser.add_argument(
        "command",
        nargs="?",
        default="interactive",
        choices=["interactive", "run", "demo-budget", "demo-failure", "demo-threshold"],
    )
    parser.add_argument("--base-dir", default=str(Path.cwd()))
    parser.add_argument("--pool", action="store_true", help="demo-failure 使用真实 spawn 进程池")
    args = parser.parse_args(argv)
    base = Path(args.base_dir)
    _, tasks = _workspace(base)
    snapshot_dir = base / "var" / "snapshots"
    try:
        if args.command == "interactive":
            run_interactive(base)
        elif args.command == "run":
            service = WorkflowService(default_config(), snapshot_dir)
            _print_summary(service.run(tasks, run_id="cli-run", in_process=True))
        elif args.command == "demo-budget":
            _print_summary(demo_budget_conflict(tasks, snapshot_dir / "budget-demo"))
        elif args.command == "demo-failure":
            _print_summary(
                demo_worker_failure(
                    tasks,
                    snapshot_dir / "failure-demo",
                    in_process=not args.pool,
                )
            )
        elif args.command == "demo-threshold":
            _print_threshold(demo_threshold_stability())
    except BusinessError as exc:
        print(f"[业务错误] {exc.code}: {exc.message} 上下文={exc.context}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
