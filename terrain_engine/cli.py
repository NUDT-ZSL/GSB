"""Command line entry point.

Sub-commands:

* ``render``   validate a task and render all tiles (with optional edits);
* ``preview``  render/serve a single tile using the persistent cache;
* ``verify``   validate every cached tile digest and report drift.

All machine-readable output is JSON.  Graceful termination (SIGINT/SIGTERM
on POSIX, SIGINT on Windows) is deferred to tile boundaries and exits with
code 130 plus an interrupt marker in the state directory.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import edits as edit_mod
from . import engine as engine_mod
from . import errors, scheduler, storage
from .edits import load_edits
from .validate import load_task


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, indent=2))
    sys.stdout.write("\n")


def _emit_error(exc: errors.EngineError) -> int:
    _emit(exc.to_dict())
    return 2


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum: int, frame: Any) -> None:  # pragma: no cover
        stop_event.set()

    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            continue


def _resolve(path_text: str, base: Path) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else base / path


def _apply_edits(task_path: Path, task, args: argparse.Namespace) -> List:
    edits_arg = getattr(args, "edits", None)
    if not edits_arg:
        return []
    edit_path = _resolve(edits_arg, Path.cwd())
    edit_list = load_edits(edit_path)
    up_to_batch = getattr(args, "batch", None)
    if up_to_batch is None:
        up_to_batch = max((edit.batch for edit in edit_list), default=0)
    edit_mod.apply_batches(task.terrain, edit_list, up_to_batch)
    return [edit for edit in edit_list if edit.batch <= up_to_batch]


def _load_edit_manifest(args: argparse.Namespace):
    edits_arg = getattr(args, "edits", None)
    if not edits_arg:
        return []
    return load_edits(_resolve(edits_arg, Path.cwd()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terrain-engine",
        description="Tiled deterministic off-screen terrain renderer")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=".",
                        help="workspace root for relative state/output paths")
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", parents=[common],
                            help="render all tiles")
    render.add_argument("task", help="path to task description JSON")
    render.add_argument("--edits", help="optional edit manifest JSON")
    render.add_argument("--batch", type=int,
                        help="apply edits up to this batch id")
    render.add_argument("--injections", help="exception injection JSON")
    render.add_argument("--image", default="outputs/image.pgm")
    render.add_argument("--depth", default="outputs/depth.f32")
    render.add_argument("--report", default="outputs/report.json")
    render.add_argument("--no-publish", action="store_true",
                        help="skip final composite publication")

    append = sub.add_parser(
        "append",
        parents=[common],
        help="append edit batches and re-render only invalidated tiles")
    append.add_argument("task")
    append.add_argument("--edits", required=True)
    append.add_argument("--batch", type=int,
                        help="new high-water batch (default: max in manifest)")
    append.add_argument("--injections")
    append.add_argument("--image", default="outputs/image.pgm")
    append.add_argument("--depth", default="outputs/depth.f32")
    append.add_argument("--report", default="outputs/append_report.json")

    preview = sub.add_parser("preview", parents=[common],
                             help="render one tile")
    preview.add_argument("task")
    preview.add_argument("tile_id")
    preview.add_argument("--injections")

    verify = sub.add_parser("verify", parents=[common],
                            help="verify cached tile digests")
    verify.add_argument("task")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    task_path = Path(args.task)
    if not task_path.is_absolute():
        task_path = (Path.cwd() / task_path).resolve()

    try:
        # Full validation happens before any output directory is created.
        task = load_task(task_path)
        # Plain render applies edits up-front so the version id matches.
        edit_list = _apply_edits(task_path, task, args) \
            if args.command != "append" else []
        injections = engine_mod.load_injections(
            _resolve(getattr(args, "injections", None), Path.cwd())
            if getattr(args, "injections", None) else None)

        stop_event = threading.Event()
        _install_signal_handlers(stop_event)

        if args.command == "append":
            all_edits = _load_edit_manifest(args)
            cursor = storage.read_edit_cursor(task, root)
            last_batch = int(cursor.get("last_batch", -1))
            # Restore the previously edited height field so older batches are
            # never re-applied in a new process.
            if last_batch >= 0:
                snapshot = storage.read_terrain_snapshot(task, root)
                if snapshot is None:
                    raise errors.fail(
                        errors.E_STATE,
                        "edit cursor exists but terrain snapshot is missing")
                task = replace(
                    task,
                    terrain=replace(task.terrain, heights=snapshot))
            target_batch = getattr(args, "batch", None)
            if target_batch is None:
                target_batch = max(
                    (edit.batch for edit in all_edits), default=last_batch)
            if target_batch <= last_batch:
                raise errors.fail(
                    errors.E_SCHEMA,
                    "append batch must be greater than the applied cursor",
                    last_batch=last_batch, requested=target_batch)
            new_edits = [
                edit for edit in all_edits
                if last_batch < edit.batch <= target_batch]
            runner = engine_mod.Engine(
                task, root, injections=injections,
                stop_event=stop_event)
            report_payload = runner.incremental(task.terrain, new_edits)
            storage.write_edit_cursor(task, root, {
                "last_batch": target_batch,
                "applied_edit_seqs": [edit.seq for edit in new_edits],
                "version": runner.version,
            })
            storage.write_terrain_snapshot(task, root)
            image_path = _resolve(args.image, root)
            depth_path = _resolve(args.depth, root)
            image_path.parent.mkdir(parents=True, exist_ok=True)
            depth_path.parent.mkdir(parents=True, exist_ok=True)
            report_payload["publication"] = runner.publish(
                image_path, depth_path)
            report_path = _resolve(args.report, root)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = report_path.with_name(report_path.name + ".tmp")
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump({"ok": True, "report": report_payload},
                          handle, sort_keys=True, indent=2)
                handle.flush()
            os.replace(tmp, report_path)
            _emit({"ok": True, "report_path": str(report_path)})
            return 0

        runner = engine_mod.Engine(
            task, root, injections=injections, stop_event=stop_event)

        if args.command == "preview":
            _emit({"ok": True, "result": runner.preview(args.tile_id)})
            return 0

        if args.command == "verify":
            results = []
            for tile in task.tiles:
                try:
                    stored = storage.load_tile(
                        task, root, tile, runner.version)
                    results.append({
                        "tile": tile.tile_id,
                        "ok": stored is not None,
                        "present": stored is not None})
                except errors.EngineError as exc:
                    results.append({
                        "tile": tile.tile_id, "ok": False,
                        "error_code": exc.code,
                        "error_message": exc.message})
            _emit({"ok": True, "results": results,
                   "version": runner.version})
            return 0

        report_payload = runner.render_all()

        if report_payload.get("interrupted"):
            _emit({"ok": True, "interrupted": True,
                   "exit_code": scheduler.GRACEFUL_EXIT_CODE,
                   "report": report_payload})
            return scheduler.GRACEFUL_EXIT_CODE

        if not args.no_publish:
            image_path = _resolve(args.image, root)
            depth_path = _resolve(args.depth, root)
            image_path.parent.mkdir(parents=True, exist_ok=True)
            depth_path.parent.mkdir(parents=True, exist_ok=True)
            publication = runner.publish(image_path, depth_path)
            report_payload["publication"] = publication

        report_path = _resolve(args.report, root)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = report_path.with_name(report_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump({"ok": True, "report": report_payload},
                      handle, sort_keys=True, indent=2)
            handle.flush()
        os.replace(tmp, report_path)
        _emit({"ok": True, "report_path": str(report_path)})
        return 0

    except errors.EngineError as exc:
        return _emit_error(exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

