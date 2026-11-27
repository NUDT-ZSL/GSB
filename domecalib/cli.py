"""Command line interface sharing the same kernel as the Tkinter GUI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from .core.engine import CalibrationEngine
from .core.errors import CalibrationError
from .core.persistence import (
    save_project,
    load_project,
    save_snapshot,
    load_snapshot,
    list_snapshots,
)
from .core.samplegen import generate_sample_project
from .core.visualization import (
    encode_ppm,
    encode_pgm,
    lut_visualization,
    mask_visualization,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                               default=_json_default), encoding="utf-8")


def _json_default(obj):
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def _print_error(exc: CalibrationError) -> int:
    print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
    return 2


def cmd_generate(args) -> int:
    project = generate_sample_project(seed=args.seed)
    path = save_project(project, Path(args.out) / "project.json")
    print(json.dumps({"status": "ok", "project": str(path),
                      "projectors": [p.projector_id for p in project.projectors],
                      "seed": args.seed}, ensure_ascii=False))
    return 0


def _load_or_generate(args) -> CalibrationEngine:
    if args.project:
        project = load_project(args.project)
    else:
        project = generate_sample_project(seed=args.seed)
    return CalibrationEngine(project)


def cmd_calibrate(args) -> int:
    engine = _load_or_generate(args)
    report = engine.full_rebuild()
    out_dir = Path(args.out)
    save_project(engine.project, out_dir / "project.json")
    _write_json(out_dir / "report.json", report)
    for pid, texture in engine.composite_textures().items():
        np.save(out_dir / f"texture_{pid}.npy", texture)
    preview, _, _ = engine.preview()
    (out_dir / "preview.ppm").write_bytes(encode_ppm(preview))
    label = args.snapshot or "baseline"
    save_snapshot(engine.project, args.snapshot_dir, label)
    truth = report["alignment"]["truth_comparison"]
    worst = max(v["max_angular_error_rad"] for v in truth.values())
    print(json.dumps({
        "status": "ok",
        "report": str(out_dir / "report.json"),
        "snapshot": label,
        "worst_reverse_map_error_rad": worst,
        "brightness_cv": report["brightness"]["uniformity_cv_post"],
        "versions": report["versions"],
    }, ensure_ascii=False, default=_json_default))
    return 0


def cmd_report(args) -> int:
    project = load_snapshot(args.snapshot_dir, args.label)
    engine = CalibrationEngine(project)
    report = engine.run_reports()
    _write_json(Path(args.out) / "report.json", report)
    print(json.dumps({"status": "ok", "report": args.out}, ensure_ascii=False))
    return 0


def cmd_export_views(args) -> int:
    project = load_snapshot(args.snapshot_dir, args.label)
    engine = CalibrationEngine(project)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for pid, lut in engine.project.luts.items():
        (out_dir / f"lut_{pid}.ppm").write_bytes(encode_ppm(lut_visualization(lut)))
        (out_dir / f"mask_{pid}.pgm").write_bytes(
            encode_pgm(engine.project.masks[pid].weight)
        )
        (out_dir / f"mask_{pid}.ppm").write_bytes(
            encode_ppm(mask_visualization(engine.project.masks[pid]))
        )
    preview, _, _ = engine.preview()
    (out_dir / "preview.ppm").write_bytes(encode_ppm(preview))
    print(json.dumps({"status": "ok", "out_dir": str(out_dir)}, ensure_ascii=False))
    return 0


def cmd_adjust_blend(args) -> int:
    project = load_snapshot(args.snapshot_dir, args.label)
    engine = CalibrationEngine(project)
    report = engine.update_blending_params(
        feather_width_deg=args.feather_width,
        curve=args.curve,
        gamma=args.gamma,
    )
    save_snapshot(engine.project, args.snapshot_dir, args.new_label)
    _write_json(Path(args.out) / "report.json", report)
    print(json.dumps({"status": "ok", "new_snapshot": args.new_label,
                      "brightness_cv": report["brightness"]["uniformity_cv_post"]},
                     ensure_ascii=False, default=_json_default))
    return 0


def cmd_incremental(args) -> int:
    """Requirement 6: incremental vs full rebuild must match pixel-by-pixel."""
    base = load_project(args.project) if args.project else generate_sample_project(args.seed)

    full_engine = CalibrationEngine(base)
    full_engine.full_rebuild()

    inc_engine = CalibrationEngine(load_project(args.project)) if args.project \
        else CalibrationEngine(generate_sample_project(args.seed))
    inc_engine.rebuild_luts()
    inc_engine.rebuild_blending()

    target = base.projectors[0].projector_id
    spec = inc_engine.project.projector(target)
    before = spec.fov_h_deg
    inc_engine.update_projector(target, fov_h_deg=before + args.delta_fov)

    # Independent full rebuild of the same modified project.
    ref_engine = CalibrationEngine(inc_engine.clone().project)
    ref_engine.full_rebuild()

    mismatches = []
    for pid in ref_engine.project.luts:
        a, b = inc_engine.project.luts[pid], ref_engine.project.luts[pid]
        if not np.array_equal(a.valid, b.valid):
            mismatches.append({"projector": pid, "artefact": "valid_mask"})
            continue
        for name in ("lon", "lat", "raw_lon", "raw_lat"):
            if not np.allclose(getattr(a, name)[a.valid], getattr(b, name)[b.valid],
                               atol=1e-12, rtol=0.0):
                mismatches.append({"projector": pid, "artefact": name})
        ma, mb = inc_engine.project.masks[pid], ref_engine.project.masks[pid]
        if not np.allclose(ma.weight, mb.weight, atol=1e-12):
            mismatches.append({"projector": pid, "artefact": "blend_weight"})

    result = {"status": "ok" if not mismatches else "mismatch",
              "mismatches": mismatches,
              "changed_projector": target,
              "incremental_versions": inc_engine.project.projector_versions}
    print(json.dumps(result, ensure_ascii=False, default=_json_default))
    return 0 if not mismatches else 1


def cmd_rollback(args) -> int:
    snapshots = list_snapshots(args.snapshot_dir)
    labels = [s["label"] for s in snapshots]
    if args.label not in labels:
        raise CalibrationError("E1008_SNAPSHOT_IO",
                               f"snapshot '{args.label}' not found",
                               {"available": labels})
    project = load_snapshot(args.snapshot_dir, args.label)
    save_project(project, Path(args.out) / "project.json")
    print(json.dumps({"status": "ok", "rolled_back_to": args.label,
                      "versions": project.projector_versions}, ensure_ascii=False))
    return 0


def cmd_error_demo(args) -> int:
    """Exercise diagnostic error codes without corrupting existing data."""
    from .core.geometry import validate_distortion
    from .core.mapping import fit_tps, MIN_CONTROL_POINTS
    from .core.models import ControlPointSet

    results = []
    # 1. Insufficient control points.
    few = ControlPointSet("PX", np.zeros(3), np.zeros(3), np.zeros(3),
                          np.zeros(3), 0.0)
    try:
        fit_tps(few)
    except CalibrationError as exc:
        results.append(exc.to_dict())

    # 2. Near collinear points.
    n = MIN_CONTROL_POINTS + 2
    x = np.linspace(0.0, 1.0, n)
    collinear = ControlPointSet("PX", x, x * 1e-12, x, np.zeros(n), 0.0)
    try:
        fit_tps(collinear)
    except CalibrationError as exc:
        results.append(exc.to_dict())

    # 3. Distortion out of range.
    try:
        validate_distortion(0.9, 0.0, 1.5)
    except CalibrationError as exc:
        results.append(exc.to_dict())

    print(json.dumps({"status": "ok", "errors": results}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domecalib",
        description="Dome projection geometric calibration and edge blending",
    )
    parser.add_argument("--seed", type=int, default=20261128)
    parser.add_argument("--project", help="path to a saved project.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="generate a fixed-seed sample project")
    p.add_argument("--out", default="samples")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("calibrate", help="full calibration + blending + reports")
    p.add_argument("--out", default="samples/out")
    p.add_argument("--snapshot-dir", default="snapshots")
    p.add_argument("--snapshot", default="baseline")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("report", help="rebuild reports from a snapshot")
    p.add_argument("--snapshot-dir", default="snapshots")
    p.add_argument("--label", default="baseline")
    p.add_argument("--out", default="samples/out")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("export-views", help="export LUT/mask/preview images")
    p.add_argument("--snapshot-dir", default="snapshots")
    p.add_argument("--label", default="baseline")
    p.add_argument("--out", default="samples/out/views")
    p.set_defaults(func=cmd_export_views)

    p = sub.add_parser("adjust-blend", help="change feather parameters")
    p.add_argument("--snapshot-dir", default="snapshots")
    p.add_argument("--label", default="baseline")
    p.add_argument("--new-label", default="adjusted")
    p.add_argument("--feather-width", type=float, default=None)
    p.add_argument("--curve", choices=["linear", "smoothstep"], default=None)
    p.add_argument("--gamma", type=float, default=None)
    p.add_argument("--out", default="samples/out")
    p.set_defaults(func=cmd_adjust_blend)

    p = sub.add_parser("incremental-check",
                       help="verify incremental vs full rebuild equality")
    p.add_argument("--delta-fov", type=float, default=1.5)
    p.set_defaults(func=cmd_incremental)

    p = sub.add_parser("rollback", help="roll back to a saved snapshot")
    p.add_argument("--snapshot-dir", default="snapshots")
    p.add_argument("--label", required=True)
    p.add_argument("--out", default="samples/rollback")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("error-demo", help="show diagnostic business error codes")
    p.set_defaults(func=cmd_error_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CalibrationError as exc:
        return _print_error(exc)


if __name__ == "__main__":
    raise SystemExit(main())
