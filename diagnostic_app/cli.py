from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from .acceptance import run_acceptance
from .demo import grid_progress, run_demo
from .errors import AppError, ErrorLedger, error_response
from .planner import plan_blocks
from .samples import generate_all
from .selfcheck import run_cross_module_self_check
from .service import DiagnosticService


HELP_TEXT = """
命令:
  tasks                     列出本地图文对任务
  use <task_id>             切换当前任务
  plan [task_id]            计算但不提交块网格计划
  submit [task_id]          整体替换并提交块计划快照
  center [task_id]          优先查询中心区域块
  block <index> [task_id]   查询指定块
  rest [task_id]            并发查询其余块
  progress                  显示块就绪网格
  verify [task_id]          逐字节比对分块拼接与全局生成
  selfcheck [task_id]       运行缓存键/哈希/独立复算/覆盖审计
  snapshots                 显示三类快照版本与校验和
  errors                    显示最近业务错误码
  acceptance                运行完整本地验收
  help                      显示帮助
  quit | exit               退出
""".strip()


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def build_shell_service(samples_dir: Path, workspace: Path) -> DiagnosticService:
    if not (samples_dir / "tasks.jsonl").exists():
        generate_all(samples_dir)
    ledger = ErrorLedger(workspace / "error_ledger.jsonl")
    service = DiagnosticService(
        workspace=workspace / "runtime",
        tasks_path=samples_dir / "tasks.jsonl",
        error_ledger=ledger,
    )
    for task_id in service.tasks:
        reference = samples_dir / "references" / f"{task_id}.npy"
        if reference.exists():
            service.set_reference(task_id, reference)
    return service


def run_interactive(samples_dir: str = "samples", workspace: str = "artifacts/interactive") -> int:
    samples_path = Path(samples_dir)
    workspace_path = Path(workspace)
    service = build_shell_service(samples_path, workspace_path)
    current_task = next(iter(service.tasks), None)
    print("图文对条件生成诊断台已启动。输入 help 查看命令，quit 退出。")
    if current_task:
        print(f"当前任务: {current_task}")

    while True:
        try:
            line = input("diagnostic> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not line:
            continue
        try:
            parts = shlex.split(line)
            command, args = parts[0].lower(), parts[1:]
            if command in ("quit", "exit"):
                print("再见。")
                return 0
            if command == "help":
                print(HELP_TEXT)
            elif command == "tasks":
                for task_id, task in service.tasks.items():
                    shape = task["image_shape"]
                    marker = "*" if task_id == current_task else " "
                    print(f"{marker} {task_id}: shape={shape}, steps={task['sampler_steps']}, "
                          f"profile={task['precision_profile']}")
            elif command == "use":
                if not args:
                    raise AppError("E_INVALID_REQUEST", "usage: use <task_id>")
                current_task = service.get_task(args[0])["task_id"]
                print(f"当前任务切换为 {current_task}")
            elif command == "plan":
                task_id = args[0] if args else current_task
                plan = plan_blocks(service.get_task(task_id), service.config)
                _print_json({
                    "grid": plan["grid"],
                    "center_block_index": plan["center_block_index"],
                    "selected_estimate": plan["selected_estimate"],
                    "adjudication": plan["adjudication"],
                })
            elif command == "submit":
                task_id = args[0] if args else current_task
                result = service.submit_plan(task_id)
                current_task = task_id
                print(f"已提交 {task_id}: grid={result['plan']['grid']}, "
                      f"plan v{result['plan_snapshot']['version']}")
            elif command == "center":
                task_id = args[0] if args else current_task
                result = service.preview_center(task_id, anchor_strength=0.18)
                current_task = task_id
                print(f"中心块 {result['block_index']} {result['bounds']['half_open_text']} 就绪")
                _print_json(result["timing"])
                print(grid_progress(service.active_plan, service.completed_block_indexes()))
            elif command == "block":
                if not args:
                    raise AppError("E_INVALID_REQUEST", "usage: block <index> [task_id]")
                block_index = int(args[0])
                task_id = args[1] if len(args) > 1 else current_task
                result = service.preview_block(task_id, block_index, anchor_strength=0.18)
                print(f"块 {result['block_index']} {result['bounds']['half_open_text']} 就绪, "
                      f"hash={result['product_hash'][:16]}...")
                print(grid_progress(service.active_plan, service.completed_block_indexes()))
            elif command == "rest":
                task_id = args[0] if args else current_task
                result = service.preview_remaining(task_id, anchor_strength=0.18)
                _print_json({k: v for k, v in result.items() if k != "successes"})
                print(grid_progress(service.active_plan, service.completed_block_indexes()))
            elif command == "progress":
                if service.active_plan is None:
                    print("尚无活动计划；先运行 center 或 submit。")
                else:
                    print(grid_progress(service.active_plan, service.completed_block_indexes()))
                    print(f"ready={service.completed_block_indexes()}")
            elif command == "verify":
                task_id = args[0] if args else current_task
                result = service.verify_blocks(task_id, anchor_strength=0.18)
                print(f"一致性: bytewise={result['bytewise_equal']}, digest={result['global_digest']}")
            elif command == "selfcheck":
                task_id = args[0] if args else current_task
                _print_json(run_cross_module_self_check(service, task_id))
            elif command == "snapshots":
                _print_json({
                    kind: None if envelope is None else {
                        "version": envelope["version"],
                        "checksum": envelope["checksum"],
                        "logical_ms": envelope["logical_ms"],
                    }
                    for kind, envelope in service.snapshots.load_all().items()
                })
            elif command == "errors":
                records = service.errors.all()[-10:]
                _print_json(records)
            elif command == "acceptance":
                report = run_acceptance("artifacts/acceptance", str(samples_path))
                print(f"acceptance ok={report['ok']}; report={report['report_path']}")
            else:
                print(f"未知命令: {command}; 输入 help")
        except AppError as error:
            _print_json(error_response(error))
        except (ValueError, KeyError) as error:
            _print_json(error_response(AppError("E_INVALID_REQUEST", str(error))))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diagnostic-app", description="确定性图文对条件生成诊断应用")
    subparsers = parser.add_subparsers(dest="command")

    generate = subparsers.add_parser("generate-samples", help="生成固定种子样本")
    generate.add_argument("--root", default="samples")

    acceptance = subparsers.add_parser("acceptance", help="运行本地验收并输出 JSON/Markdown 账本")
    acceptance.add_argument("--output", default="artifacts/acceptance")
    acceptance.add_argument("--samples", default="samples")

    demo = subparsers.add_parser("demo", help="运行非交互脚本化演示")
    demo.add_argument("--samples", default="samples")
    demo.add_argument("--workspace", default="artifacts/demo")
    demo.add_argument("--task", default="tdc-002")

    interactive = subparsers.add_parser("interactive", help="启动交互式诊断台")
    interactive.add_argument("--samples", default="samples")
    interactive.add_argument("--workspace", default="artifacts/interactive")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "generate-samples":
        manifest = generate_all(args.root)
        print(json.dumps({"ok": True, "task_count": manifest["task_count"],
                          "manifest": "samples/manifest.json"}, ensure_ascii=False))
        return 0
    if args.command == "acceptance":
        report = run_acceptance(args.output, args.samples)
        print(json.dumps({
            "ok": report["ok"],
            "core_ok": report["core_ok"],
            "scenario_ok": report["scenario_ok"],
            "missing_error_codes": report["missing_error_codes"],
            "report_path": report["report_path"],
            "markdown_path": report["markdown_path"],
        }, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if args.command == "demo":
        report = run_demo(args.samples, args.workspace, args.task)
        return 0 if report["ok"] else 1
    if args.command == "interactive":
        return run_interactive(args.samples, args.workspace)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
