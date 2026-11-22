from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .demo import run_recovery_demo
from .edits import (
    add_template,
    edit_quality_weight,
    edit_sample_output,
    remove_template,
    set_quota_target,
    set_total_target,
)
from .engine import PipelineEngine, pipeline_status
from .errors import BusinessError, E_INPUT_INVALID
from .generator import generate_dataset_files
from .reports import record_error, write_json_report
from .stages import STAGE_NAMES
from .validation import load_bundle
from .workspace import Workspace


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="workspace root")
    parser.add_argument("--json", action="store_true", help="emit JSON output")


def _dump(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _short_ids(identifiers: list[str], limit: int = 16) -> str:
    if not identifiers:
        return "-"
    if len(identifiers) <= limit:
        return ", ".join(identifiers)
    return ", ".join(identifiers[:limit]) + f" ... (+{len(identifiers) - limit})"


def format_status(status: dict[str, Any]) -> str:
    lines = [
        f"当前阶段: {status['current_stage']}   下一阶段: {status['next_stage']}",
        f"下一个全局版本: {status['next_version']}   状态代数: {status['generation']}",
        "阶段快照:",
    ]
    for stage in status["stages"]:
        marker = "✓" if stage["committed"] else "·"
        version = stage["version"] if stage["version"] is not None else "-"
        lines.append(
            f"  {marker} [{stage['index']}] {stage['stage']:<9} v={version:<3} "
            f"processed={stage['processed_count']:<3} "
            f"completed={stage['completed_count']:<3} "
            f"output={stage['output_count']:<3} {stage['label']}"
        )
    export = status.get("export")
    if export and "error" not in export:
        lines.append(
            f"导出: {export['path']} records={export['record_count']} "
            f"bytes_sha256={export['dataset_bytes_checksum']}"
        )
    edit = status.get("last_edit")
    if edit:
        lines.append(
            "最近编辑重算: "
            + (", ".join(edit.get("recomputed_stages", [])) or "-")
        )
        lines.append(
            "最近编辑复用快照: "
            + (", ".join(edit.get("reused_snapshot_stages", [])) or "-")
        )
        affected = edit.get("affected_sample_ids", {})
        for stage, identifiers in affected.items():
            lines.append(f"  {stage} 受影响样本: {_short_ids(identifiers)}")
    recovery = status.get("recovery")
    if recovery:
        lines.append(
            f"恢复报告: mode={recovery.get('mode')} "
            f"started={recovery.get('started_with_stages')} "
            f"invalidated_from={recovery.get('invalidated_from')} "
            f"complete={recovery.get('completed')}"
        )
        lines.append(
            "  恢复重算集合: "
            + (", ".join(recovery.get("recomputed_stages", [])) or "-")
        )
        lines.append(
            "  恢复复用快照: "
            + (", ".join(recovery.get("reused_snapshot_stages", [])) or "-")
        )
    demo = status.get("demo")
    if demo:
        lines.append(
            f"最近演示: mode={demo.get('mode')} kill_stage={demo.get('kill_stage')} "
            f"byte_identical={demo.get('byte_identical')}"
        )
    error = status.get("last_error")
    if error:
        lines.append(
            f"最近业务错误: {error.get('code')} field={error.get('field')} "
            f"stage={error.get('stage')} message={error.get('message')}"
        )
    return "\n".join(lines)


def summarize_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": run["ok"],
        "mode": run["mode"],
        "started_with_stages": run["started_with_stages"],
        "recomputed_stages": run["recomputed_stages"],
        "cache_hit_stages": run["cache_hit_stages"],
        "reused_snapshot_stages": run["reused_snapshot_stages"],
        "invalidated_from": run["invalidated_from"],
        "content_changed_stages": run["content_changed_stages"],
        "degradations": run["degradations"],
        "stopped_gracefully": run["stopped_gracefully"],
        "completed": run["completed"],
        "final_export": run["final_export"],
        "stages": [
            {
                "stage": stage["stage"],
                "action": stage["action"],
                "version": stage["version"],
                "processed_count": stage["processed_count"],
                "output_count": stage["output_count"],
                "estimated_memory_mb": stage["estimated_memory_mb"],
            }
            for stage in run["stage_reports"]
        ],
        "stage_diffs": {
            stage: {
                "old_version": diff["old_version"],
                "new_version": diff["new_version"],
                "content_changed": diff["content_changed"],
                "affected_sample_ids": diff["affected_sample_ids"],
                "reused_sample_ids": diff["reused_sample_ids"],
            }
            for stage, diff in run["stage_diffs"].items()
        },
    }


def _run_edit(
    workspace: Workspace,
    edit_result: dict[str, Any],
    *,
    edit_request: dict[str, Any],
) -> dict[str, Any]:
    engine = PipelineEngine(workspace)
    run = engine.run()
    run_dict = run.to_dict()
    report = {
        "ok": True,
        "edit_request": edit_request,
        "edit_result": edit_result,
        "mode": run.mode,
        "recomputed_stages": run.recomputed_stages,
        "cache_hit_stages": run.cache_hit_stages,
        "reused_snapshot_stages": run.reused_snapshot_stages,
        "invalidated_from": run.invalidated_from,
        "content_changed_stages": run.content_changed_stages,
        "affected_sample_ids": {
            stage: diff["affected_sample_ids"]
            for stage, diff in run.stage_diffs.items()
            if diff["affected_sample_ids"]
        },
        "reused_sample_ids": {
            stage: diff["reused_sample_ids"]
            for stage, diff in run.stage_diffs.items()
            if diff["reused_sample_ids"]
        },
        "stage_diffs": run_dict["stage_diffs"],
        "final_export": run_dict["final_export"],
    }
    write_json_report(workspace.last_edit_path, report)
    return report


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    workspace.ensure_layout()
    if args.reset_state:
        shutil.rmtree(workspace.state_dir, ignore_errors=True)
        shutil.rmtree(workspace.exports_dir, ignore_errors=True)
        shutil.rmtree(workspace.root / "_demo", ignore_errors=True)
        workspace.ensure_layout()
    generated = generate_dataset_files(workspace, force=args.force or args.reset_state)
    bundle = load_bundle(workspace)
    PipelineEngine(workspace).store.ensure_manifest()
    return {
        "ok": True,
        "version": __version__,
        "workspace": str(workspace.root),
        "generation": generated,
        "validated_sample_count": len(bundle.samples),
        "state_reset": bool(args.reset_state),
    }


def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    engine = PipelineEngine(workspace)
    result = engine.run()
    return summarize_run(result.to_dict())


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    return pipeline_status(workspace)


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    return PipelineEngine(workspace).self_check()


def cmd_demo(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    return run_recovery_demo(
        workspace, kill_stage=args.kill_stage, mode=args.mode
    )


def cmd_edit_output(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    edit = edit_sample_output(workspace, args.sample_id, args.output)
    return _run_edit(
        workspace,
        edit,
        edit_request={
            "edit": "sample_output",
            "sample_id": args.sample_id,
        },
    )


def cmd_edit_weight(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    edit = edit_quality_weight(workspace, args.weight, args.value)
    return _run_edit(
        workspace,
        edit,
        edit_request={
            "edit": "quality_weight",
            "weight": args.weight,
            "value": args.value,
        },
    )


def cmd_add_template(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    if args.template_file:
        template = json.loads(Path(args.template_file).read_text(encoding="utf-8"))
    else:
        template = json.loads(args.template_json)
    if not isinstance(template, dict):
        raise BusinessError(
            "E_TEMPLATE_INVALID",
            "template JSON must be an object",
            field="templates",
        )
    edit = add_template(workspace, template)
    return _run_edit(
        workspace,
        edit,
        edit_request={"edit": "add_template", "template_id": template.get("template_id")},
    )


def cmd_remove_template(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    edit = remove_template(workspace, args.template_id)
    return _run_edit(
        workspace,
        edit,
        edit_request={"edit": "remove_template", "template_id": args.template_id},
    )


def cmd_edit_quota(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    edit = set_quota_target(
        workspace, args.source_tag, args.quality_tier, args.target
    )
    return _run_edit(
        workspace,
        edit,
        edit_request={
            "edit": "quota_target",
            "source_tag": args.source_tag,
            "quality_tier": args.quality_tier,
            "target": args.target,
        },
    )


def cmd_edit_total(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    edit = set_total_target(workspace, args.target)
    return _run_edit(
        workspace,
        edit,
        edit_request={"edit": "total_target", "target": args.target},
    )


def cmd_show_export(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.root)
    engine = PipelineEngine(workspace)
    state = engine.store.load_manifest()
    if "export" not in state["stages"]:
        raise BusinessError(
            "E_STAGE_ORDER", "export stage is not committed", stage="export"
        )
    payload = engine.store.read_snapshot(
        "export", state["stages"]["export"]
    )["payload"]
    path = workspace.root / payload["export_path"]
    head: list[dict[str, Any]] = []
    if path.is_file():
        with path.open("r", encoding="utf-8") as handle:
            for _ in range(args.head):
                line = handle.readline()
                if not line:
                    break
                head.append(json.loads(line))
    return {
        "ok": True,
        "path": payload["export_path"],
        "content_checksum": payload["content_checksum"],
        "dataset_bytes_checksum": payload["dataset_bytes_checksum"],
        "record_count": payload["record_count"],
        "header": payload["header"],
        "head": head,
    }


def _worker_stop_requested(workspace: Workspace, event: threading.Event) -> bool:
    return event.is_set() or workspace.stop_request_path.exists()


def cmd_worker(args: argparse.Namespace) -> int:
    workspace = Workspace(args.root)
    stop_event = threading.Event()
    try:
        workspace.stop_request_path.unlink()
    except FileNotFoundError:
        pass

    def handle_signal(signum: int, frame: Any) -> None:
        stop_event.set()

    for signal_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        candidate = getattr(signal, signal_name, None)
        if candidate is not None:
            try:
                signal.signal(candidate, handle_signal)
            except (OSError, ValueError):
                pass

    def progress(stage: str, done: int, total: int) -> None:
        if (
            args.fault_stage == stage
            and args.fault_after_items is not None
            and done >= args.fault_after_items
        ):
            print(f"FAULT_EXIT:{stage}:{done}/{total}", flush=True)
            os._exit(137)

    announced = False

    def after_stage(stage: str, entry: dict[str, Any]) -> bool:
        nonlocal announced
        if args.pause_after_stage == stage and not announced:
            announced = True
            print(
                f"AFTER_COMMIT:{stage}:v{entry['version']}",
                flush=True,
            )
            while not _worker_stop_requested(workspace, stop_event):
                time.sleep(0.02)
            if _worker_stop_requested(workspace, stop_event):
                print(f"GRACEFUL_STOP:{stage}", flush=True)
                return True
        if _worker_stop_requested(workspace, stop_event):
            print(f"GRACEFUL_STOP:{stage}", flush=True)
            return True
        return False

    try:
        result = PipelineEngine(workspace).run(
            progress_hook=progress, after_stage_hook=after_stage
        )
        print(
            "WORKER_DONE:"
            + json.dumps(
                {
                    "completed": result.completed,
                    "stopped_gracefully": result.stopped_gracefully,
                    "stages": result.recomputed_stages,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    except BusinessError as exc:
        record_error(workspace, exc)
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True),
            flush=True,
        )
        return 2


def _interactive_status(workspace: Workspace) -> None:
    print()
    print(format_status(pipeline_status(workspace)))
    print()


def cmd_interactive(args: argparse.Namespace) -> int:
    workspace = Workspace(args.root)
    if not workspace.inputs_exist():
        print("输入不存在，先执行初始化。")
        cmd_init(argparse.Namespace(root=args.root, force=False, reset_state=False))
    PipelineEngine(workspace).store.ensure_manifest()
    while True:
        _interactive_status(workspace)
        print(
            "1) 继续/恢复构造   2) 强制终止恢复演示   3) 可处理信号演示\n"
            "4) 修改样本输出     5) 调整清洗权重       6) 添加模板\n"
            "7) 删除模板         8) 修改配额目标       9) 自检\n"
            "10) 查看导出头      0) 退出"
        )
        try:
            choice = input("选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        try:
            if choice == "0":
                return 0
            if choice == "1":
                _dump(summarize_run(PipelineEngine(workspace).run().to_dict()), False)
            elif choice == "2":
                _dump(
                    run_recovery_demo(workspace, kill_stage="quality", mode="kill"),
                    False,
                )
            elif choice == "3":
                _dump(
                    run_recovery_demo(workspace, kill_stage="quality", mode="term"),
                    False,
                )
            elif choice == "4":
                sample_id = input("sample_id: ").strip()
                output = input("新 output: ")
                edit = edit_sample_output(workspace, sample_id, output)
                _dump(
                    _run_edit(
                        workspace,
                        edit,
                        edit_request={
                            "edit": "sample_output",
                            "sample_id": sample_id,
                        },
                    ),
                    False,
                )
            elif choice == "5":
                weight = input("权重名 length/repetition/noise: ").strip()
                value = float(input("新权重: ").strip())
                edit = edit_quality_weight(workspace, weight, value)
                _dump(
                    _run_edit(
                        workspace,
                        edit,
                        edit_request={
                            "edit": "quality_weight",
                            "weight": weight,
                            "value": value,
                        },
                    ),
                    False,
                )
            elif choice == "6":
                raw = input("模板 JSON（单行）: ").strip()
                template = json.loads(raw)
                edit = add_template(workspace, template)
                _dump(
                    _run_edit(
                        workspace,
                        edit,
                        edit_request={
                            "edit": "add_template",
                            "template_id": template.get("template_id"),
                        },
                    ),
                    False,
                )
            elif choice == "7":
                template_id = input("template_id: ").strip()
                edit = remove_template(workspace, template_id)
                _dump(
                    _run_edit(
                        workspace,
                        edit,
                        edit_request={
                            "edit": "remove_template",
                            "template_id": template_id,
                        },
                    ),
                    False,
                )
            elif choice == "8":
                source_tag = input("source_tag: ").strip()
                quality_tier = input("quality_tier: ").strip()
                target = int(input("target: ").strip())
                edit = set_quota_target(
                    workspace, source_tag, quality_tier, target
                )
                _dump(
                    _run_edit(
                        workspace,
                        edit,
                        edit_request={
                            "edit": "quota_target",
                            "source_tag": source_tag,
                            "quality_tier": quality_tier,
                            "target": target,
                        },
                    ),
                    False,
                )
            elif choice == "9":
                _dump(PipelineEngine(workspace).self_check(), False)
            elif choice == "10":
                _dump(
                    cmd_show_export(
                        argparse.Namespace(root=args.root, head=3)
                    ),
                    False,
                )
            else:
                print("未知选项")
        except BusinessError as exc:
            record_error(workspace, exc)
            _dump(exc.as_dict(), False)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"输入错误: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sft_pipeline",
        description="Local deterministic LLM SFT data construction pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="generate/validate local inputs")
    _add_common(init_parser)
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--reset-state", action="store_true")
    init_parser.set_defaults(func=cmd_init)

    run_parser = subparsers.add_parser("run", help="run or resume the pipeline")
    _add_common(run_parser)
    run_parser.set_defaults(func=cmd_run)

    status_parser = subparsers.add_parser("status", help="show pipeline state")
    _add_common(status_parser)
    status_parser.set_defaults(func=cmd_status)

    check_parser = subparsers.add_parser("check", help="run consistency self-check")
    _add_common(check_parser)
    check_parser.set_defaults(func=cmd_check)

    demo_parser = subparsers.add_parser("demo", help="run kill/restart comparison demo")
    _add_common(demo_parser)
    demo_parser.add_argument("--kill-stage", default="quality", choices=STAGE_NAMES)
    demo_parser.add_argument("--mode", default="kill", choices=("kill", "term"))
    demo_parser.set_defaults(func=cmd_demo)

    interactive_parser = subparsers.add_parser("interactive", help="interactive terminal UI")
    _add_common(interactive_parser)
    interactive_parser.set_defaults(func=cmd_interactive)

    output_parser = subparsers.add_parser("edit-output", help="edit one sample output")
    _add_common(output_parser)
    output_parser.add_argument("sample_id")
    output_parser.add_argument("--output", required=True)
    output_parser.set_defaults(func=cmd_edit_output)

    weight_parser = subparsers.add_parser("edit-weight", help="edit quality weight")
    _add_common(weight_parser)
    weight_parser.add_argument("weight", choices=("length", "repetition", "noise"))
    weight_parser.add_argument("value", type=float)
    weight_parser.set_defaults(func=cmd_edit_weight)

    add_template_parser = subparsers.add_parser("add-template", help="append a template")
    _add_common(add_template_parser)
    group = add_template_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--template-json")
    group.add_argument("--template-file")
    add_template_parser.set_defaults(func=cmd_add_template)

    remove_template_parser = subparsers.add_parser(
        "remove-template", help="remove a template"
    )
    _add_common(remove_template_parser)
    remove_template_parser.add_argument("template_id")
    remove_template_parser.set_defaults(func=cmd_remove_template)

    quota_parser = subparsers.add_parser("edit-quota", help="set a quota target")
    _add_common(quota_parser)
    quota_parser.add_argument("source_tag")
    quota_parser.add_argument("quality_tier")
    quota_parser.add_argument("target", type=int)
    quota_parser.set_defaults(func=cmd_edit_quota)

    total_parser = subparsers.add_parser("edit-total", help="set total sample target")
    _add_common(total_parser)
    total_parser.add_argument("target", type=int)
    total_parser.set_defaults(func=cmd_edit_total)

    export_parser = subparsers.add_parser("show-export", help="show export header")
    _add_common(export_parser)
    export_parser.add_argument("--head", type=int, default=3)
    export_parser.set_defaults(func=cmd_show_export)

    worker_parser = subparsers.add_parser("worker", help=argparse.SUPPRESS)
    _add_common(worker_parser)
    worker_parser.add_argument("--pause-after-stage", choices=STAGE_NAMES)
    worker_parser.add_argument("--fault-stage", choices=STAGE_NAMES)
    worker_parser.add_argument("--fault-after-items", type=int)
    worker_parser.set_defaults(func=cmd_worker)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        result = args.func(args)
        if isinstance(result, int):
            return result
        if args.command == "status" and not as_json:
            print(format_status(result))
        else:
            _dump(result, as_json)
        return 0
    except BusinessError as exc:
        workspace = Workspace(getattr(args, "root", "."))
        try:
            record_error(workspace, exc)
        except OSError:
            pass
        if as_json:
            print(json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        error = BusinessError(
            E_INPUT_INVALID,
            str(exc),
            field="arguments",
        )
        workspace = Workspace(getattr(args, "root", "."))
        try:
            record_error(workspace, error)
        except OSError:
            pass
        if as_json:
            print(json.dumps(error.as_dict(), ensure_ascii=False), file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
