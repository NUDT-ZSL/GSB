from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import (
    ROUNDING_MODES,
    SUBNORMAL_MODES,
    PipelineConfig,
)
from .demo import run_demo
from .engine import PipelineEngine
from .errors import PipelineError
from .jobs import generate_sample_jobs, jobs_fingerprint, write_jobs_jsonl
from .status import build_status
from .verify import verify_run


def _emit(payload: Any, *, stream: Any = None) -> None:
    target = stream or sys.stdout
    target.write(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    target.flush()


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--atol", type=float, default=1e-10)
    parser.add_argument("--rtol", type=float, default=1e-10)
    parser.add_argument("--max-stage-jobs", type=int, default=10_000)
    parser.add_argument("--max-blocks", type=int, default=200_000)
    parser.add_argument("--max-accum-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--subnormal-mode", choices=SUBNORMAL_MODES, default="preserve")
    parser.add_argument("--rounding-mode", choices=ROUNDING_MODES, default="nearest")


def _config_from_args(args: argparse.Namespace) -> PipelineConfig:
    return PipelineConfig(
        atol=args.atol,
        rtol=args.rtol,
        max_stage_jobs=args.max_stage_jobs,
        max_block_count=args.max_blocks,
        max_accum_bytes=args.max_accum_bytes,
        subnormal_mode=args.subnormal_mode,
        rounding_mode=args.rounding_mode,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmm-pipeline",
        description="确定性 GEMM/BMM 四阶段流水、快照恢复与一致性验证",
    )
    subparsers = parser.add_subparsers(dest="command")

    generate = subparsers.add_parser("generate", help="生成确定性 JSONL 样本任务")
    generate.add_argument("--out", required=True)
    generate.add_argument("--seed", type=int, default=20261122)

    run = subparsers.add_parser("run", help="执行或恢复四阶段流水")
    run.add_argument("--jobs", required=True)
    run.add_argument("--run-dir", required=True)
    run.add_argument(
        "--fault-after-blocks",
        type=int,
        default=None,
        help="累加阶段完成指定分块数后硬退出，用于故障注入",
    )
    _add_config_arguments(run)

    status = subparsers.add_parser("status", help="展示当前阶段、快照、计数与错误")
    status.add_argument("--run-dir", required=True)
    status.add_argument("--recent-errors", type=int, default=10)

    verify = subparsers.add_parser("verify", help="执行跨模块一致性自检")
    verify.add_argument("--run-dir", required=True)

    demo = subparsers.add_parser("demo", help="强制终止后恢复并对比完整运行")
    demo.add_argument("--work-dir", default=".demo")
    demo.add_argument("--jobs", default=None)
    demo.add_argument("--fault-after-blocks", type=int, default=20)
    _add_config_arguments(demo)

    interactive = subparsers.add_parser("interactive", help="交互式状态与验证面板")
    interactive.add_argument("--run-dir", required=True)
    return parser


def _command_generate(args: argparse.Namespace) -> int:
    jobs = generate_sample_jobs(args.seed)
    write_jobs_jsonl(args.out, jobs)
    _emit(
        {
            "jobs_file": str(Path(args.out)),
            "job_count": len(jobs),
            "jobset_fingerprint": jobs_fingerprint(jobs),
            "seed": args.seed,
        }
    )
    return 0


def _command_run(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    engine = PipelineEngine(args.run_dir, config)
    fault_hook = None
    if args.fault_after_blocks is not None:
        threshold = args.fault_after_blocks

        def fault_hook(event: dict[str, Any]) -> None:
            if int(event["blocks_done"]) >= threshold:
                _emit(
                    {
                        "event": "injected_forced_termination",
                        "stage": event["stage"],
                        "job_id": event["job_id"],
                        "blocks_done": event["blocks_done"],
                        "total_blocks": event["total_blocks"],
                    }
                )
                os._exit(97)

    try:
        report = engine.run(args.jobs, fault_hook=fault_hook)
    except PipelineError as exc:
        _emit(exc.to_dict(), stream=sys.stderr)
        return 2
    _emit(report.to_dict())
    return 0


def _command_status(args: argparse.Namespace) -> int:
    try:
        payload = build_status(
            args.run_dir, recent_error_limit=args.recent_errors
        )
    except PipelineError as exc:
        _emit(exc.to_dict(), stream=sys.stderr)
        return 2
    _emit(payload)
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    try:
        report = verify_run(args.run_dir)
    except PipelineError as exc:
        PipelineEngine(args.run_dir).record_error(exc)
        _emit(exc.to_dict(), stream=sys.stderr)
        return 2
    _emit(report.to_dict())
    if report.ok:
        return 0
    try:
        report.raise_if_failed()
    except PipelineError as exc:
        PipelineEngine(args.run_dir).record_error(exc)
        _emit(exc.to_dict(), stream=sys.stderr)
    return 2


def _command_demo(args: argparse.Namespace) -> int:
    try:
        report = run_demo(
            args.work_dir,
            config=_config_from_args(args),
            jobs_path=args.jobs,
            fault_after_blocks=args.fault_after_blocks,
        )
    except PipelineError as exc:
        _emit(exc.to_dict(), stream=sys.stderr)
        return 2
    _emit(report)
    return 0


def _interactive_banner(run_dir: str) -> None:
    print(f"BMM 阶段恢复验证面板，run_dir={run_dir}")
    print("命令: status | verify | snapshots | errors [N] | help | quit")


def _command_interactive(args: argparse.Namespace) -> int:
    _interactive_banner(args.run_dir)
    while True:
        try:
            line = input("bmm> ").strip()
        except EOFError:
            print()
            return 0
        if not line:
            continue
        parts = line.split()
        command = parts[0].lower()
        if command in {"quit", "exit", "q"}:
            return 0
        if command == "help":
            _interactive_banner(args.run_dir)
            continue
        if command == "status":
            try:
                _emit(build_status(args.run_dir, recent_error_limit=10))
            except PipelineError as exc:
                _emit(exc.to_dict(), stream=sys.stderr)
            continue
        if command == "snapshots":
            try:
                status = build_status(args.run_dir)
                _emit(status["snapshots"])
            except PipelineError as exc:
                _emit(exc.to_dict(), stream=sys.stderr)
            continue
        if command == "errors":
            limit = 10
            if len(parts) > 1:
                try:
                    limit = max(1, int(parts[1]))
                except ValueError:
                    print("errors 的参数必须是整数")
                    continue
            try:
                _emit(build_status(args.run_dir, recent_error_limit=limit)["recent_errors"])
            except PipelineError as exc:
                _emit(exc.to_dict(), stream=sys.stderr)
            continue
        if command == "verify":
            try:
                report = verify_run(args.run_dir)
            except PipelineError as exc:
                PipelineEngine(args.run_dir).record_error(exc)
                _emit(exc.to_dict(), stream=sys.stderr)
                continue
            _emit(
                {
                    "ok": report.ok,
                    "checks_run": report.checks_run,
                    "issue_count": len(report.issues),
                    "issues": report.issues[:20],
                }
            )
            continue
        print(f"未知命令: {command}; 输入 help 查看命令")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    dispatch = {
        "generate": _command_generate,
        "run": _command_run,
        "status": _command_status,
        "verify": _command_verify,
        "demo": _command_demo,
        "interactive": _command_interactive,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
