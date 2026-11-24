from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import numpy as np

from . import EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, __version__
from .errors import WorkbenchError
from .evaluation import build_evaluation_report, export_mask, export_report
from .project import Project
from .sample_data import generate_sample


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _project(task: str | Path) -> Project:
    return Project.from_task(task)


def _parse_chunk(value: str, project: Project) -> str:
    text = str(value)
    if text in project.chunks_by_id:
        return text
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 3:
        raise WorkbenchError("E_REVISION", "--chunk must be id or z,y,x")
    try:
        index = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise WorkbenchError("E_REVISION", "chunk indices must be integers") from exc
    chunk_id = f"{index[0]:03d}_{index[1]:03d}_{index[2]:03d}"
    if chunk_id not in project.chunks_by_id:
        raise WorkbenchError("E_REVISION", f"unknown chunk {chunk_id}")
    return chunk_id


def _global_box_to_local_mask(
    project: Project, chunk_id: str, box_values: Sequence[int]
) -> tuple[np.ndarray, np.ndarray]:
    if len(box_values) != 6:
        raise WorkbenchError("E_REVISION", "--box expects z0,z1,y0,y1,x0,x1")
    z0, z1, y0, y1, x0, x1 = (int(v) for v in box_values)
    chunk = project.chunks_by_id[chunk_id]
    core = chunk.core
    if z0 < core.z0 or z1 > core.z1 or y0 < core.y0 or y1 > core.y1 or x0 < core.x0 or x1 > core.x1:
        raise WorkbenchError(
            "E_REVISION",
            "revision box must lie inside the selected chunk core",
            details={"core_box": core.to_tuple()},
        )
    edit_mask = np.zeros(core.shape, dtype=bool)
    edit_mask[z0 - core.z0 : z1 - core.z0, y0 - core.y0 : y1 - core.y0, x0 - core.x0 : x1 - core.x0] = True
    return edit_mask, edit_mask


def command_validate(args: argparse.Namespace) -> int:
    project = _project(args.task)
    payload = {
        "ok": True,
        "version_id": project.version_id,
        "volume_digest": project.volume_digest,
        "shape": list(project.cfg.shape),
        "grid_count": list(project.cfg.grid_count),
        "chunk_count": project.cfg.chunk_count,
        "morph_radius": project.cfg.morph_radius,
        "parallelism": project.cfg.parallelism,
        "pixel_memory_budget": project.cfg.pixel_memory_budget,
        "logic_time_budget_ms": project.cfg.logic_time_budget_ms,
        "chunks": [
            {
                "chunk_id": chunk.id,
                "core_box": chunk.core.to_tuple(),
                "buffer_slots": __import__("medseg_workbench.segmentation", fromlist=["buffer_slot_estimate"]).buffer_slot_estimate(project.cfg, chunk),
            }
            for chunk in project.chunks
        ],
    }
    print_json(payload)
    return EXIT_OK


def command_generate(args: argparse.Namespace) -> int:
    info = generate_sample(args.out, seed=args.seed, force=args.force)
    print_json({"ok": True, **info})
    return EXIT_OK


def command_run(args: argparse.Namespace) -> int:
    project = _project(args.task)
    stop_event = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        stop_event.set()

    for signame in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            try:
                signal.signal(sig, handle_signal)
            except (ValueError, OSError):
                pass

    report = project.run(
        stop_event=stop_event,
        chunk_real_delay=float(args.chunk_delay_seconds or 0.0),
    )
    print_json(report)
    return EXIT_INTERRUPTED if report.get("interrupted") else EXIT_OK


def command_revise(args: argparse.Namespace) -> int:
    project = _project(args.task).prepare()
    chunk_id = _parse_chunk(args.chunk, project)
    layer = "raw" if args.layer == "raw" else "final_mask"
    apply_edit = project.apply_revision if layer == "raw" else project.apply_final_revision
    if args.demo or (not args.box and not args.point):
        revision = project.demo_revision(chunk_id, layer=layer)
    elif args.box:
        values = [int(v) for v in args.box.split(",")]
        edit_mask, _ = _global_box_to_local_mask(project, chunk_id, values)
        desired = np.full(project.chunks_by_id[chunk_id].core.shape, int(args.value), dtype=np.uint8)
        revision = apply_edit(chunk_id, edit_mask, desired, source="cli", note=args.note or "")
    else:
        z, y, x = (int(v) for v in args.point.split(","))
        chunk = project.chunks_by_id[chunk_id]
        radius = max(0, int(args.radius))
        edit_mask = np.zeros(chunk.core.shape, dtype=bool)
        lz, ly, lx = z - chunk.core.z0, y - chunk.core.y0, x - chunk.core.x0
        dz, dy, dx = np.ogrid[: chunk.core.shape[0], : chunk.core.shape[1], : chunk.core.shape[2]]
        edit_mask = (dz - lz) ** 2 + (dy - ly) ** 2 + (dx - lx) ** 2 <= radius**2
        desired = np.full(chunk.core.shape, int(args.value), dtype=np.uint8)
        revision = apply_edit(chunk_id, edit_mask, desired, source="cli", note=args.note or "")

    invalidation = project.invalidation_plan()
    payload = {
        "ok": True,
        "revision": {"chunk_id": revision.chunk_id, "layer": layer, "digest": revision.digest(), "history": revision.history},
        "invalidation": invalidation,
    }
    if args.recompute:
        payload["run"] = project.run()
    print_json(payload)
    return EXIT_OK


def command_invalidate(args: argparse.Namespace) -> int:
    project = _project(args.task)
    print_json({"ok": True, **project.invalidation_plan()})
    return EXIT_OK


def command_states(args: argparse.Namespace) -> int:
    project = _project(args.task)
    print_json({"ok": True, "states": project.chunk_states()})
    return EXIT_OK


def command_evaluate(args: argparse.Namespace) -> int:
    project = _project(args.task).prepare()
    report = build_evaluation_report(project)
    if args.export:
        export_info = export_report(report, args.export, force=args.force)
        report["report_export"] = export_info
    print_json(report)
    return EXIT_OK


def command_export(args: argparse.Namespace) -> int:
    project = _project(args.task).prepare()
    mask_info = export_mask(project, args.mask_out, force=args.force)
    report = build_evaluation_report(project)
    report_info = export_report(report, args.report_out or project.cfg.export_dir / "report.json", force=args.force)
    print_json({"ok": True, "mask_export": mask_info, "report_export": report_info})
    return EXIT_OK


def command_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui

    return run_gui(args.task)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="medseg-workbench", description="Chunked segmentation and mask editing workbench")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-sample", help="generate deterministic local sample")
    gen.add_argument("--out", type=Path, default=Path("samples/demo"))
    gen.add_argument("--seed", type=int, default=20261125)
    gen.add_argument("--force", action="store_true")
    gen.set_defaults(func=command_generate)

    validate = sub.add_parser("validate", help="validate task JSON and volume slices without writing outputs")
    validate.add_argument("--task", required=True)
    validate.set_defaults(func=command_validate)

    run = sub.add_parser("run", help="run scheduler and persist chunk results")
    run.add_argument("--task", required=True)
    run.add_argument("--chunk-delay-seconds", type=float, default=0.0)
    run.set_defaults(func=command_run)

    revise = sub.add_parser("revise", help="apply an operator raw-mask revision")
    revise.add_argument("--task", required=True)
    revise.add_argument("--chunk", required=True)
    revise.add_argument("--box", help="global half-open z0,z1,y0,y1,x0,x1")
    revise.add_argument("--point", help="global z,y,x")
    revise.add_argument("--radius", type=int, default=1)
    revise.add_argument("--value", type=int, choices=(0, 1), default=0)
    revise.add_argument("--layer", choices=("final", "raw"), default="final", help="final=WYSIWYG authoritative mask edit; raw=pre-morph growth correction")
    revise.add_argument("--demo", action="store_true")
    revise.add_argument("--note", default="")
    revise.add_argument("--recompute", action="store_true")
    revise.set_defaults(func=command_revise)

    inv = sub.add_parser("invalidate", help="show dependency invalidation plan")
    inv.add_argument("--task", required=True)
    inv.set_defaults(func=command_invalidate)

    states = sub.add_parser("states", help="show per-chunk persisted state")
    states.add_argument("--task", required=True)
    states.set_defaults(func=command_states)

    evaluate = sub.add_parser("evaluate", help="evaluate against truth for reporting only")
    evaluate.add_argument("--task", required=True)
    evaluate.add_argument("--export")
    evaluate.add_argument("--force", action="store_true")
    evaluate.set_defaults(func=command_evaluate)

    export = sub.add_parser("export", help="atomically export mask and evaluation report")
    export.add_argument("--task", required=True)
    export.add_argument("--mask-out")
    export.add_argument("--report-out")
    export.add_argument("--force", action="store_true")
    export.set_defaults(func=command_export)

    gui = sub.add_parser("gui", help="launch PyQt5 diagnostic workbench")
    gui.add_argument("--task", nargs="?")
    gui.set_defaults(func=command_gui)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except WorkbenchError as exc:
        print_json(exc.to_dict())
        return int(exc.exit_code or EXIT_ERROR)
    except KeyboardInterrupt:
        print_json({"ok": False, "error": {"code": "E_INTERRUPTED", "message": "interrupted at chunk boundary"}})
        return EXIT_INTERRUPTED
    except Exception as exc:
        print_json({"ok": False, "error": {"code": "E_INTERNAL", "message": f"{type(exc).__name__}: {exc}"}})
        return 1


if __name__ == "__main__":
    sys.exit(main())
