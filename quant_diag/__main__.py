from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .config import EngineConfig, Injection
from .demo import ensure_local_samples, run_demo
from .engine import DiagnosticEngine
from .errors import BusinessError
from .manifest import ManifestView, load_manifest, write_manifest
from .selfcheck import run_consistency_checks
from .snapshots import SnapshotStore
from .util import read_json


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    workspace = Path(args.workspace)
    manifest = Path(args.manifest) if args.manifest else workspace / "model_manifest.jsonl"
    calibration = (
        Path(args.calibration)
        if args.calibration
        else workspace / "calibration_samples.jsonl"
    )
    return manifest, calibration


def _config(args: argparse.Namespace) -> EngineConfig:
    values = {}
    if getattr(args, "config", None):
        values.update(read_json(Path(args.config)))
    if getattr(args, "group_size", None) is not None:
        values["group_size"] = args.group_size
    if getattr(args, "tolerance_abs", None) is not None:
        values["tolerance_abs"] = args.tolerance_abs
    if getattr(args, "tolerance_rel", None) is not None:
        values["tolerance_rel"] = args.tolerance_rel
    if getattr(args, "max_layers_per_stage", None) is not None:
        values["max_layers_per_stage"] = args.max_layers_per_stage
    if getattr(args, "max_resident_weight_mb", None) is not None:
        values["max_resident_weight_mb"] = args.max_resident_weight_mb
    if getattr(args, "max_calibration_comparisons", None) is not None:
        values["max_calibration_comparisons"] = args.max_calibration_comparisons
    injections = [Injection.parse(item) for item in getattr(args, "inject", []) or []]
    if injections:
        values["injections"] = [item.as_dict() for item in injections]
    if getattr(args, "input_override", None):
        values["input_override"] = [
            float(item) for item in args.input_override.split(",") if item.strip()
        ]
    return EngineConfig.from_mapping(values)


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _command_engine(args: argparse.Namespace) -> DiagnosticEngine:
    manifest, calibration = _paths(args)
    return DiagnosticEngine(
        manifest_path=manifest,
        calibration_path=calibration,
        runtime_dir=Path(args.runtime_dir),
        config=_config(args),
    )


def _run(args: argparse.Namespace) -> int:
    try:
        engine = _command_engine(args)
        report = engine.run()
        _print_json(report)
        return 0
    except BusinessError as exc:
        _print_json({"ok": False, "error": exc.as_dict()})
        return 2


def _inject(args: argparse.Namespace) -> int:
    if not args.inject:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "E_INVALID_REQUEST",
                    "message": "inject requires at least one --inject KIND@LAYER",
                },
            }
        )
        return 2
    return _run(args)


def _status(args: argparse.Namespace) -> int:
    manifest, calibration = _paths(args)
    ensure_local_samples(Path(args.workspace))
    store = SnapshotStore(Path(args.runtime_dir))
    try:
        snapshot = store.load_latest()
    except BusinessError as exc:
        _print_json({"ok": False, "history": store.history(), "error": exc.as_dict()})
        return 2
    if snapshot is None:
        _print_json({"ok": True, "status": "empty", "history": []})
        return 0
    inference = snapshot["stages"].get("layer_inference", {})
    diagnostics = inference.get("diagnostics", {})
    _print_json(
        {
            "ok": True,
            "version": snapshot["version"],
            "stage": snapshot["stage"],
            "model_tag": snapshot["model_tag"],
            "manifest_digest": snapshot["manifest_digest"],
            "config_digest": snapshot["config_digest"],
            "result_digest": snapshot["result_digest"],
            "completed_layers": snapshot["completed_layers"],
            "history": store.history(),
            "summary": {
                "dominant_source": diagnostics.get("overall_dominant_source"),
                "final_serial_error": diagnostics.get("final_serial_error"),
                "tolerance_passed": diagnostics.get("passed"),
            },
            "events": store.load_events(limit=10),
        }
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    try:
        engine = _command_engine(args)
        report = engine.run()
        snapshot = engine.latest_snapshot()
        result = run_consistency_checks(snapshot, engine.config)
        _print_json({"ok": result["passed"], "run": report, "self_check": result})
        return 0 if result["passed"] else 2
    except BusinessError as exc:
        _print_json({"ok": False, "error": exc.as_dict()})
        return 2


def _generate(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    if workspace.joinpath("model_manifest.jsonl").exists() and not args.force:
        print("sample files already exist; use --force to regenerate", file=sys.stderr)
        return 1
    manifest, calibration = ensure_local_samples(workspace)
    _print_json(
        {
            "ok": True,
            "manifest": str(manifest),
            "calibration": str(calibration),
            "manifest_digest": load_manifest(manifest).digest,
        }
    )
    return 0


def _kill_worker(args: argparse.Namespace) -> int:
    manifest, calibration = _paths(args)
    engine = DiagnosticEngine(
        manifest_path=manifest,
        calibration_path=calibration,
        runtime_dir=Path(args.runtime_dir),
        config=_config(args),
        force_kill_after=(args.kill_stage, args.kill_layer),
    )
    engine.run()
    return 0


def _update_manifest(args: argparse.Namespace) -> int:
    manifest, _ = _paths(args)
    view = load_manifest(manifest)
    layers = list(view.layers)
    matched = False
    for index, layer in enumerate(layers):
        if layer.layer_id == args.layer:
            layers[index] = replace(layer, weight_revision=args.revision)
            matched = True
            break
    if not matched:
        _print_json({"ok": False, "error": f"layer not found: {args.layer}"})
        return 2
    output = Path(args.output)
    write_manifest(output, ManifestView.from_layers(view.model_tag, layers))
    _print_json(
        {
            "ok": True,
            "output": str(output),
            "layer": args.layer,
            "revision": args.revision,
            "manifest_digest": load_manifest(output).digest,
        }
    )
    return 0


def _demo(args: argparse.Namespace) -> int:
    report = run_demo(
        Path(args.workspace),
        runtime_root=Path(args.runtime_root) if args.runtime_root else None,
        output_path=Path(args.output) if args.output else None,
        progress=lambda step: print(f"[demo] {step}", file=sys.stderr),
    )
    _print_json(report)
    return 0 if report["passed"] else 2


def _web(args: argparse.Namespace) -> int:
    from .web import serve

    serve(
        workspace=Path(args.workspace),
        runtime_dir=Path(args.runtime_dir),
        host=args.host,
        port=args.port,
        open_browser=args.open,
    )
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--manifest")
    parser.add_argument("--calibration")
    parser.add_argument("--runtime-dir", default="runtime/state")
    parser.add_argument("--config", help="JSON configuration file")
    parser.add_argument("--group-size", type=int)
    parser.add_argument("--tolerance-abs", type=float)
    parser.add_argument("--tolerance-rel", type=float)
    parser.add_argument("--max-layers-per-stage", type=int)
    parser.add_argument("--max-resident-weight-mb", type=float)
    parser.add_argument("--max-calibration-comparisons", type=int)
    parser.add_argument(
        "--inject",
        action="append",
        default=[],
        help="KIND@LAYER, repeatable",
    )
    parser.add_argument("--input-override", help="comma-separated inference input")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-diag",
        description="Deterministic low-precision inference diagnostics workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run or resume the four-stage pipeline")
    _add_common(run_parser)
    run_parser.set_defaults(func=_run)

    inject_parser = subparsers.add_parser(
        "inject", help="run with one or more input/parameter injections"
    )
    _add_common(inject_parser)
    inject_parser.set_defaults(func=_inject)

    status_parser = subparsers.add_parser("status", help="show latest committed snapshot")
    _add_common(status_parser)
    status_parser.set_defaults(func=_status)

    verify_parser = subparsers.add_parser("verify", help="run and execute cross-module checks")
    _add_common(verify_parser)
    verify_parser.set_defaults(func=_verify)

    generate_parser = subparsers.add_parser("generate", help="generate deterministic samples")
    generate_parser.add_argument("--workspace", default=".")
    generate_parser.add_argument("--force", action="store_true")
    generate_parser.set_defaults(func=_generate)

    kill_parser = subparsers.add_parser(
        "_kill-worker", help="internal worker used by the kill/recovery demo"
    )
    _add_common(kill_parser)
    kill_parser.add_argument("--kill-stage", required=True)
    kill_parser.add_argument("--kill-layer", type=int, required=True)
    kill_parser.set_defaults(func=_kill_worker)

    update_parser = subparsers.add_parser(
        "update-revision", help="write a manifest with a higher layer revision"
    )
    _add_common(update_parser)
    update_parser.add_argument("--layer", required=True)
    update_parser.add_argument("--revision", type=int, required=True)
    update_parser.add_argument("--output", required=True)
    update_parser.set_defaults(func=_update_manifest)

    demo_parser = subparsers.add_parser("demo", help="run the complete local acceptance demo")
    demo_parser.add_argument("--workspace", default=".")
    demo_parser.add_argument("--runtime-root")
    demo_parser.add_argument("--output")
    demo_parser.set_defaults(func=_demo)

    web_parser = subparsers.add_parser("web", help="start the local diagnostic workbench")
    web_parser.add_argument("--workspace", default=".")
    web_parser.add_argument("--runtime-dir", default="runtime/workbench")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8765)
    web_parser.add_argument("--open", action="store_true")
    web_parser.set_defaults(func=_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
