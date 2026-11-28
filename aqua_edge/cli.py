"""Command-line entry point and interactive demonstration."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Any

from .analysis import (
    analyze,
    build_suggestions,
    compare_analyses,
)
from .errors import AquaError
from .generator import (
    DEFAULT_SEED,
    generate_boundary_project,
    generate_invalid_examples,
    generate_project,
)
from .model import Project, load_project
from .policy import apply_policy, parse_assignments, parse_csv_list
from .reference import ReferenceScheduler
from .report import (
    format_compare,
    format_project,
    format_schedule,
    format_snapshot_summary,
)
from .scheduler import SNAPSHOT_VERSION, Scheduler
from .snapshot import load_snapshot, save_snapshot
from .trace import build_trace_document, compare_frame_traces, load_json, save_json


AUTO_COMMANDS = [
    "generate --kind all --out-dir samples",
    "load samples/pond_network.json",
    "schedule --json-out artifacts/full_trace.json",
    "verify samples/pond_network.json --json-out artifacts/reference_check.json",
    "snapshot save artifacts/snapshot_after_24.json --after 24",
    (
        "adjust max_streams=2 "
        "priority_order=P01/AERATOR-01,P02/AERATOR-01,P03/AERATOR-01,"
        "P01/DO,P02/DO,P03/DO,P01/PH,P02/PH,P03/PH,"
        "P01/TEMP,P02/TEMP,P03/TEMP,P02/HISTORY "
        "priority=P02/HISTORY=5 "
        "window=P01/DO=6400,P02/DO=6400,P03/DO=6400,"
        "P01/PH=4800,P02/PH=4800,P03/PH=4800,"
        "P01/TEMP=4000,P02/TEMP=4000,P03/TEMP=4000"
    ),
    "compare --json-out artifacts/policy_compare.json",
    "schedule --json-out artifacts/optimized_trace.json",
    "snapshot load artifacts/snapshot_after_24.json",
    "resume --json-out artifacts/resumed_trace.json --verify-full",
]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except AquaError as exc:
        print(_format_error(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aqua-edge",
        description="水产养殖边缘网关离线多流调度与队首阻塞分析工具（仅标准库）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="生成固定种子工程和边界样本")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--kind", choices=["sample", "boundary", "all"], default="sample")
    generate.add_argument("--out", help="sample 工程输出路径")
    generate.add_argument("--out-dir", default="samples", help="多类样本输出目录")

    validate = sub.add_parser("validate", help="只装载校验工程")
    validate.add_argument("project")

    load = sub.add_parser("load", help="装载并展示工程")
    load.add_argument("project")

    schedule = sub.add_parser("schedule", help="执行完整或截断调度")
    schedule.add_argument("project")
    schedule.add_argument("--after", type=int, help="完成至少 N 帧后停止")
    schedule.add_argument("--json-out", help="逐帧轨迹 JSON 输出路径")
    _add_policy_args(schedule)

    compare = sub.add_parser("compare", help="调整策略并比较前后时延分布")
    compare.add_argument("project")
    compare.add_argument("--json-out", help="对比结果 JSON 输出路径")
    _add_policy_args(compare)

    snap_save = sub.add_parser("snapshot-save", help="保存版本化调度快照")
    snap_save.add_argument("project")
    snap_save.add_argument("--after", type=int, required=True, help="完成至少 N 帧时保存")
    snap_save.add_argument("--out", required=True, help="快照输出路径")
    _add_policy_args(snap_save)

    snap_resume = sub.add_parser("snapshot-resume", help="加载快照并继续调度")
    snap_resume.add_argument("snapshot")
    snap_resume.add_argument("--json-out", help="续算完整轨迹输出路径")
    snap_resume.add_argument("--verify-full", action="store_true", help="与不中断全量重算逐帧比对")

    verify = sub.add_parser("verify", help="与独立参考调度器逐帧比对")
    verify.add_argument("project")
    verify.add_argument("--json-out", help="逐帧核对结果输出路径")

    demo = sub.add_parser("demo", help="交互式终端演示")
    demo.add_argument("--commands", help="分号或换行分隔的命令序列")
    demo.add_argument("--commands-file", help="命令序列文件")
    demo.add_argument("--auto", action="store_true", help="运行内置完整演示序列")
    return parser


def _add_policy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-streams", type=int, help="调整 network.max_concurrent_streams")
    parser.add_argument("--priority-order", help="最高到最低优先级流标识，逗号分隔")
    parser.add_argument("--priority", help="id=整数，逗号分隔")
    parser.add_argument("--window", help="id=字节数，逗号分隔")
    parser.add_argument("--window-default", type=int, help="统一设置所有流窗口")


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "generate":
        _generate_command(args)
        return 0
    if args.command == "validate":
        project = load_project(args.project)
        print(f"OK: 工程可装载，{len(project.streams)} 条流，{project.total_frames} 帧")
        return 0
    if args.command == "load":
        project = load_project(args.project)
        print(format_project(project))
        return 0
    if args.command == "schedule":
        project = _project_with_policy(load_project(args.project), args)
        scheduler = Scheduler(project).run(args.after)
        stats = analyze(project, scheduler)
        document = build_trace_document(project, scheduler, stats)
        if args.json_out:
            save_json(args.json_out, document)
            print(f"轨迹已写入: {args.json_out}")
        print(format_schedule(project, scheduler, stats))
        return 0
    if args.command == "compare":
        before = load_project(args.project)
        after = _project_with_policy(before, args)
        before_scheduler = Scheduler(before).run()
        after_scheduler = Scheduler(after).run()
        before_stats = analyze(before, before_scheduler)
        after_stats = analyze(after, after_scheduler)
        comparison = compare_analyses(before, before_stats, after, after_stats)
        suggestions = build_suggestions(before, before_stats, after, after_stats)
        document = {
            "compare_version": 1,
            "tool": "aqua-edge",
            "before": build_trace_document(before, before_scheduler, before_stats),
            "after": build_trace_document(after, after_scheduler, after_stats),
            "comparison": comparison,
            "suggestions": suggestions,
        }
        if args.json_out:
            save_json(args.json_out, document)
            print(f"策略对比已写入: {args.json_out}")
        print(format_compare(comparison, suggestions))
        return 0
    if args.command == "snapshot-save":
        project = _project_with_policy(load_project(args.project), args)
        scheduler = Scheduler(project).run(args.after)
        save_snapshot(scheduler, args.out)
        print(format_snapshot_summary(project, scheduler, args.out))
        return 0
    if args.command == "snapshot-resume":
        return _snapshot_resume(args)
    if args.command == "verify":
        return _verify_command(args.project, args.json_out)
    if args.command == "demo":
        return _demo_command(args)
    raise AquaError("E_ARGUMENT", f"未知命令: {args.command}")


def _generate_command(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if args.kind in {"sample", "all"}:
        path = Path(args.out) if args.out and args.kind == "sample" else out_dir / "pond_network.json"
        save_json(path, generate_project(args.seed))
        written.append(path)
    if args.kind in {"boundary", "all"}:
        path = out_dir / "boundary_network.json"
        save_json(path, generate_boundary_project())
        written.append(path)
    if args.kind == "all":
        for name, document in generate_invalid_examples().items():
            path = out_dir / f"{name}.json"
            save_json(path, document)
            written.append(path)
    for path in written:
        print(f"已生成: {path}")


def _project_with_policy(project: Project, args: argparse.Namespace) -> Project:
    return apply_policy(
        project,
        max_streams=getattr(args, "max_streams", None),
        priority_order=parse_csv_list(getattr(args, "priority_order", None)),
        priorities=parse_assignments(getattr(args, "priority", None)),
        windows=parse_assignments(getattr(args, "window", None)),
        window_default=getattr(args, "window_default", None),
    )


def _snapshot_resume(args: argparse.Namespace) -> int:
    scheduler, raw_snapshot = load_snapshot(args.snapshot)
    project = scheduler.project
    scheduler.resume()
    stats = analyze(project, scheduler)
    document = build_trace_document(project, scheduler, stats)

    verification = None
    if args.verify_full:
        full = Scheduler(project).run()
        verification = compare_frame_traces(scheduler.frame_traces(), full.frame_traces())
        document["resume_verification"] = verification

    if args.json_out:
        save_json(args.json_out, document)
        print(f"续算轨迹已写入: {args.json_out}")
    print(format_schedule(project, scheduler, stats))
    if verification is not None:
        print("")
        print(
            "续算一致性: "
            + ("逐帧完全一致" if verification["identical"] else "存在差异")
        )
        print(
            f"比较帧数: {verification['compared_frames']}，"
            f"差异帧数: {verification['different_frames']}"
        )
        if verification["first_difference"]:
            print(f"首个差异: {verification['first_difference']}")
    return 0 if verification is None or verification["identical"] else 1


def _verify_command(project_path: str, json_out: str | None = None) -> int:
    project = load_project(project_path)
    actual = Scheduler(project).run().frame_traces()
    reference = ReferenceScheduler(project).run()
    comparison = compare_frame_traces(actual, reference)
    if json_out:
        save_json(
            json_out,
            {
                "verify_version": 1,
                "tool": "aqua-edge",
                "project": project.to_dict(),
                "comparison": comparison,
                "actual_frames": actual,
                "reference_frames": reference,
            },
        )
        print(f"参考核对结果已写入: {json_out}")
    print("独立参考调度器逐帧核对：")
    print(f"工程: {project.name}")
    print(f"帧数: {comparison['compared_frames']}")
    print(f"结果: {'一致' if comparison['identical'] else '不一致'}")
    print(f"差异帧数: {comparison['different_frames']}")
    if comparison["first_difference"]:
        print(f"首个差异: {comparison['first_difference']}")
    return 0 if comparison["identical"] else 1


def _format_error(error: AquaError) -> str:
    lines = [f"[拒绝] {error.code}: {error.message}"]
    if error.code == "E_SNAPVERSION":
        lines.append(f"期望版本: {SNAPSHOT_VERSION}")
    if error.details:
        details = ", ".join(f"{key}={value}" for key, value in error.details.items())
        lines.append(f"详情: {details}")
    return "\n".join(lines)


class DemoSession:
    def __init__(self) -> None:
        self.base_project: Project | None = None
        self.project: Project | None = None
        self.scheduler: Scheduler | None = None
        self.stats: dict[str, Any] | None = None
        self.error_count = 0

    def execute_line(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return True
        try:
            tokens = shlex.split(stripped)
        except ValueError as exc:
            print(f"[拒绝] E_ARGUMENT: 命令引号不匹配: {exc}", file=sys.stderr)
            self.error_count += 1
            return True
        command = tokens[0].lower()
        arguments = tokens[1:]
        try:
            if command in {"quit", "exit"}:
                return False
            if command == "help":
                print(HELP_TEXT)
            elif command == "generate":
                self._generate(arguments)
            elif command == "load":
                self._load(arguments)
            elif command in {"show", "info"}:
                self._require_project()
                print(format_project(self.project))
            elif command == "schedule":
                self._schedule(arguments)
            elif command == "adjust":
                self._adjust(arguments)
            elif command == "compare":
                self._compare(arguments)
            elif command == "verify":
                self._verify(arguments)
            elif command == "snapshot":
                self._snapshot(arguments)
            elif command in {"resume", "continue"}:
                self._resume(arguments)
            else:
                raise AquaError("E_ARGUMENT", f"未知交互命令: {command}")
        except AquaError as exc:
            print(_format_error(exc), file=sys.stderr)
            self.error_count += 1
        except SystemExit:
            print("[拒绝] E_ARGUMENT: 命令参数不合法", file=sys.stderr)
            self.error_count += 1
        return True

    def _generate(self, arguments: list[str]) -> None:
        parser = argparse.ArgumentParser(prog="generate", add_help=False)
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
        parser.add_argument("--kind", choices=["sample", "boundary", "all"], default="sample")
        parser.add_argument("--out")
        parser.add_argument("--out-dir", default="samples")
        args = parser.parse_args(arguments)
        _generate_command(args)

    def _load(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            raise AquaError("E_ARGUMENT", "用法: load <工程.json>")
        project = load_project(arguments[0])
        self.base_project = project
        self.project = project
        self.scheduler = None
        self.stats = None
        print(f"已装载: {arguments[0]}")
        print(format_project(project))

    def _schedule(self, arguments: list[str]) -> None:
        project = self._require_project()
        parser = argparse.ArgumentParser(prog="schedule", add_help=False)
        parser.add_argument("--json-out")
        parser.add_argument("--after", type=int)
        args = parser.parse_args(arguments)
        scheduler = Scheduler(project).run(args.after)
        stats = analyze(project, scheduler)
        self.scheduler = scheduler
        self.stats = stats
        if args.json_out:
            save_json(args.json_out, build_trace_document(project, scheduler, stats))
            print(f"轨迹已写入: {args.json_out}")
        print(format_schedule(project, scheduler, stats))

    def _adjust(self, arguments: list[str]) -> None:
        project = self._require_project()
        options: dict[str, Any] = {
            "max_streams": None,
            "priority_order": None,
            "priorities": None,
            "windows": None,
            "window_default": None,
        }
        for item in arguments:
            if "=" not in item:
                raise AquaError("E_ARGUMENT", "adjust 参数必须为 key=value", {"item": item})
            key, value = item.split("=", 1)
            if key == "max_streams":
                try:
                    options["max_streams"] = int(value)
                except ValueError as exc:
                    raise AquaError(
                        "E_ARGUMENT",
                        "max_streams 必须是整数",
                        {"value": value},
                    ) from exc
            elif key == "priority_order":
                options["priority_order"] = parse_csv_list(value)
            elif key == "priority":
                options["priorities"] = parse_assignments(value)
            elif key == "window":
                options["windows"] = parse_assignments(value)
            elif key == "window_default":
                try:
                    options["window_default"] = int(value)
                except ValueError as exc:
                    raise AquaError(
                        "E_ARGUMENT",
                        "window_default 必须是整数",
                        {"value": value},
                    ) from exc
            else:
                raise AquaError("E_ARGUMENT", f"未知 adjust 参数: {key}")
        if not any(options.values()):
            raise AquaError("E_ARGUMENT", "adjust 至少需要一个调整项")
        self.project = apply_policy(project, **options)
        print("策略已调整：")
        print(
            f"max_concurrent_streams={self.project.network.max_concurrent_streams}；"
            f"优先级={{{', '.join(f'{s.stream_id}:{s.priority}' for s in self.project.streams)}}}；"
            f"窗口={{{', '.join(f'{s.stream_id}:{s.window_bytes}' for s in self.project.streams)}}}"
        )

    def _compare(self, arguments: list[str]) -> None:
        if self.base_project is None:
            raise AquaError("E_STATE", "请先 load 工程")
        parser = argparse.ArgumentParser(prog="compare", add_help=False)
        parser.add_argument("--json-out")
        args = parser.parse_args(arguments)
        before = self.base_project
        after = self._require_project()
        before_scheduler = Scheduler(before).run()
        after_scheduler = Scheduler(after).run()
        before_stats = analyze(before, before_scheduler)
        after_stats = analyze(after, after_scheduler)
        comparison = compare_analyses(before, before_stats, after, after_stats)
        suggestions = build_suggestions(before, before_stats, after, after_stats)
        if args.json_out:
            save_json(
                args.json_out,
                {
                    "compare_version": 1,
                    "tool": "aqua-edge",
                    "before": build_trace_document(before, before_scheduler, before_stats),
                    "after": build_trace_document(after, after_scheduler, after_stats),
                    "comparison": comparison,
                    "suggestions": suggestions,
                },
            )
            print(f"策略对比已写入: {args.json_out}")
        print(format_compare(comparison, suggestions))

    def _verify(self, arguments: list[str]) -> None:
        parser = argparse.ArgumentParser(prog="verify", add_help=False)
        parser.add_argument("project")
        parser.add_argument("--json-out")
        args = parser.parse_args(arguments)
        if _verify_command(args.project, args.json_out) != 0:
            raise AquaError("E_VERIFY", "独立参考调度器逐帧核对不一致")

    def _snapshot(self, arguments: list[str]) -> None:
        if not arguments:
            raise AquaError("E_ARGUMENT", "用法: snapshot save|load ...")
        action = arguments[0]
        if action == "save":
            parser = argparse.ArgumentParser(prog="snapshot save", add_help=False)
            parser.add_argument("path")
            parser.add_argument("--after", type=int, required=True)
            args = parser.parse_args(arguments[1:])
            project = self._require_project()
            scheduler = Scheduler(project).run(args.after)
            save_snapshot(scheduler, args.path)
            print(format_snapshot_summary(project, scheduler, args.path))
            return
        if action == "load":
            if len(arguments) != 2:
                raise AquaError("E_ARGUMENT", "用法: snapshot load <快照.json>")
            scheduler, _ = load_snapshot(arguments[1])
            self.project = scheduler.project
            self.base_project = scheduler.project
            self.scheduler = scheduler
            self.stats = None
            print(format_snapshot_summary(scheduler.project, scheduler, arguments[1]))
            return
        raise AquaError("E_ARGUMENT", f"未知 snapshot 动作: {action}")

    def _resume(self, arguments: list[str]) -> None:
        if self.scheduler is None:
            raise AquaError("E_STATE", "请先 snapshot load 或 schedule")
        parser = argparse.ArgumentParser(prog="resume", add_help=False)
        parser.add_argument("--json-out")
        parser.add_argument("--verify-full", action="store_true")
        args = parser.parse_args(arguments)
        project = self.scheduler.project
        self.scheduler.resume()
        stats = analyze(project, self.scheduler)
        self.stats = stats
        document = build_trace_document(project, self.scheduler, stats)
        verification = None
        if args.verify_full:
            full = Scheduler(project).run()
            verification = compare_frame_traces(
                self.scheduler.frame_traces(),
                full.frame_traces(),
            )
            document["resume_verification"] = verification
        if args.json_out:
            save_json(args.json_out, document)
            print(f"续算轨迹已写入: {args.json_out}")
        print(format_schedule(project, self.scheduler, stats))
        if verification is not None:
            print("")
            print(
                "续算一致性: "
                + ("逐帧完全一致" if verification["identical"] else "存在差异")
            )
            print(
                f"比较帧数: {verification['compared_frames']}，"
                f"差异帧数: {verification['different_frames']}"
            )
            if verification["first_difference"]:
                print(f"首个差异: {verification['first_difference']}")

    def _require_project(self) -> Project:
        if self.project is None:
            raise AquaError("E_STATE", "请先 load 工程或 snapshot load")
        return self.project


HELP_TEXT = """可用命令：
  generate [--kind sample|boundary|all] [--seed N] [--out PATH] [--out-dir DIR]
  load <工程.json>
  show
  schedule [--json-out PATH] [--after N]
  adjust max_streams=N priority_order=ID1,ID2 priority=ID=整数 window=ID=字节数 window_default=字节数
  compare [--json-out PATH]
  verify <工程.json> [--json-out PATH]
  snapshot save <快照.json> --after N
  snapshot load <快照.json>
  resume [--json-out PATH] [--verify-full]
  help
  quit
"""


def _commands_from_args(args: argparse.Namespace) -> list[str]:
    if args.auto:
        return list(AUTO_COMMANDS)
    commands: list[str] = []
    if args.commands_file:
        with Path(args.commands_file).open("r", encoding="utf-8") as handle:
            commands.extend(line.rstrip("\n") for line in handle)
    if args.commands:
        commands.extend(part.strip() for part in args.commands.replace("\n", ";").split(";"))
    return commands


def _demo_command(args: argparse.Namespace) -> int:
    session = DemoSession()
    commands = _commands_from_args(args)
    if commands:
        print("== 命令序列演示 ==")
        for command in commands:
            if not command.strip():
                continue
            print(f"\n>>> {command.strip()}")
            if not session.execute_line(command):
                break
        return 1 if session.error_count else 0

    print("== 水产边缘网关离线调度演示 ==")
    print(HELP_TEXT)
    while True:
        try:
            line = input("aqua-edge> ")
        except EOFError:
            print("")
            break
        if not session.execute_line(line):
            break
    return 1 if session.error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())

