from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any

import numpy as np

from .bundle import bundle_summary, load_bundle, write_bundle, write_revision_bundle
from .config import LabConfig
from .demo import DemoRunner
from .errors import DeployError, STAGE_LABELS
from .faults import FaultInjector
from .pipeline import Pipeline
from .store import SnapshotStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = PROJECT_ROOT / "data" / "sample"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.json"
DEFAULT_DB = PROJECT_ROOT / "artifacts" / "deploylab.sqlite"


def _load_config(path: str | None) -> LabConfig:
    if path:
        return LabConfig.load(path)
    if DEFAULT_CONFIG.exists():
        return LabConfig.load(DEFAULT_CONFIG)
    return LabConfig()


def _ensure_bundle(path: Path) -> None:
    if not (path / "manifest.jsonl").exists():
        write_bundle(path, force=True)


def _parse_injection(value: str) -> dict[str, Any]:
    parts = value.split(":")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            "injection must be STAGE:KIND[:LAYER]"
        )
    result: dict[str, Any] = {"stage": parts[0], "kind": parts[1]}
    if len(parts) >= 3 and parts[2]:
        result["layer_id"] = parts[2]
    return result


def _parse_calibration_update(value: str) -> tuple[str, dict[str, int]]:
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError(
            "calibration update must be LAYER:REVISION[:SEED]"
        )
    payload: dict[str, int] = {"revision": int(parts[1])}
    if len(parts) == 3:
        payload["seed"] = int(parts[2])
    return parts[0], payload


def _json_dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _command_run(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle)
    _ensure_bundle(bundle)
    config = _load_config(args.config)
    injections = [_parse_injection(value) for value in (args.inject or [])]
    marker = args.crash_marker or str(Path(args.db)) + ".crash_marker.json"
    faults = FaultInjector.build(
        injections,
        crash_stage=args.crash_stage,
        crash_after_layers=args.crash_after_layers,
        crash_marker_path=marker,
    )
    calibration_updates = {}
    for value in args.calibration_update or []:
        layer_id, update = _parse_calibration_update(value)
        calibration_updates[layer_id] = update
    pipeline = Pipeline(
        bundle,
        args.db,
        config=config,
        faults=faults,
        calibration_updates=calibration_updates or None,
        force_new=args.force_new,
    )
    result = pipeline.run()
    if args.json:
        _json_dump(result)
    else:
        print(
            f"OK model={result['model_tag']} generation={result['generation']} "
            f"version={result['progress_version']}"
        )
        print(f"prediction_digest={result['prediction_digest']}")
        print(
            "rebuilt="
            + json.dumps(
                result["recovery"]["rebuilt_layers_by_stage"],
                ensure_ascii=False,
            )
        )
    return 0


def _command_generate(args: argparse.Namespace) -> int:
    bundle = write_bundle(
        args.out,
        calib_samples=args.calib_samples,
        force=args.force,
    )
    summary = bundle_summary(bundle)
    if args.json:
        _json_dump(summary)
    else:
        print(
            f"generated {len(bundle.layers)} layers at {bundle.root}; "
            f"manifest={bundle.manifest_digest}"
        )
        print(f"corrupt fixtures: {bundle.root / 'corrupt'}")
    return 0


def _command_state(args: argparse.Namespace) -> int:
    pipeline = Pipeline(
        args.bundle,
        args.db,
        config=_load_config(args.config),
    )
    _json_dump(pipeline.state())
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    pipeline = Pipeline(
        args.bundle,
        args.db,
        config=_load_config(args.config),
    )
    result = pipeline.verify_consistency()
    _json_dump(result)
    return 0


def _command_revision(args: argparse.Namespace) -> int:
    bundle = write_revision_bundle(
        args.base_bundle,
        args.out,
        layer_id=args.layer,
        weight_revision=args.revision,
    )
    if args.run:
        pipeline = Pipeline(
            bundle.root,
            args.db,
            config=_load_config(args.config),
        )
        result = pipeline.run()
        _json_dump(
            {
                "bundle": bundle_summary(bundle),
                "generation": result["generation"],
                "progress_version": result["progress_version"],
                "prediction_digest": result["prediction_digest"],
                "invalidation": result["invalidation"],
                "recovery": result["recovery"],
            }
        )
    else:
        _json_dump(bundle_summary(bundle))
    return 0


def _command_calibrate(args: argparse.Namespace) -> int:
    update = {args.layer: {"revision": args.revision}}
    if args.seed is not None:
        update[args.layer]["seed"] = args.seed
    pipeline = Pipeline(
        args.bundle,
        args.db,
        config=_load_config(args.config),
        calibration_updates=update,
    )
    result = pipeline.run()
    _json_dump(
        {
            "generation": result["generation"],
            "progress_version": result["progress_version"],
            "prediction_digest": result["prediction_digest"],
            "invalidation": result["invalidation"],
            "recovery": result["recovery"],
            "consistent": pipeline.verify_consistency()["ok"],
        }
    )
    return 0


def _command_demo(args: argparse.Namespace) -> int:
    runner = DemoRunner(
        bundle_path=args.bundle,
        work_dir=args.work_dir,
        config=_load_config(args.config),
    )
    report = runner.run()
    _json_dump(report)
    return 0 if report["ok"] else 1


def _command_inject(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    for candidate in db_path.parent.glob(db_path.name + "*"):
        if candidate.is_file():
            candidate.unlink()
    bundle = Path(args.bundle)
    _ensure_bundle(bundle)
    faults = FaultInjector.build(
        [{"stage": args.stage, "kind": args.kind, "layer_id": args.layer}]
    )
    pipeline = Pipeline(
        bundle,
        db_path,
        config=_load_config(args.config),
        faults=faults,
        force_new=True,
    )
    try:
        result = pipeline.run()
    except DeployError as error:
        _json_dump(
            {
                "ok": False,
                "error": error.to_record(),
                "recent_errors": pipeline.store.recent_errors(5),
                "latest_committed_version": pipeline.store.latest_version(),
                "snapshot_stages": [
                    snapshot["stage"]
                    for snapshot in reversed(pipeline.store.trace(limit=10))
                ],
                "unattributed_count": pipeline.store.unattributed_count(),
            }
        )
        return 2
    _json_dump(
        {
            "ok": True,
            "note": "injection did not trigger",
            "prediction_digest": result["prediction_digest"],
        }
    )
    return 0


def _command_serve(args: argparse.Namespace) -> int:
    from .webapp import serve

    _ensure_bundle(Path(args.bundle))
    config = _load_config(args.config)
    url = f"http://{args.host}:{args.port}"
    if args.open:
        webbrowser.open(url)
    serve(
        host=args.host,
        port=args.port,
        bundle_path=args.bundle,
        db_path=args.db,
        config=config,
    )
    return 0


def _command_trace(args: argparse.Namespace) -> int:
    store = SnapshotStore(args.db)
    _json_dump(
        {
            "latest_version": store.latest_version(),
            "trace": store.trace(limit=args.limit),
            "chain_problems": store.verify_chain(),
            "unattributed_count": store.unattributed_count(),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploylab",
        description=(
            "Deterministic low-precision deployment, atomic progress snapshots, "
            "crash recovery and dependency invalidation."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate deterministic sample bundle")
    generate.add_argument("--out", default=str(DEFAULT_BUNDLE))
    generate.add_argument("--calib-samples", type=int, default=8)
    generate.add_argument("--force", action="store_true")
    generate.add_argument("--json", action="store_true")
    generate.set_defaults(func=_command_generate)

    run = sub.add_parser("run", help="run or resume the four-stage pipeline")
    run.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    run.add_argument("--db", default=str(DEFAULT_DB))
    run.add_argument("--config", default=None)
    run.add_argument("--json", action="store_true")
    run.add_argument("--force-new", action="store_true")
    run.add_argument("--inject", action="append", default=[])
    run.add_argument("--calibration-update", action="append", default=[])
    run.add_argument("--crash-stage", default=None)
    run.add_argument("--crash-after-layers", type=int, default=0)
    run.add_argument("--crash-marker", default=None)
    run.set_defaults(func=_command_run)

    resume = sub.add_parser("resume", help="resume from the latest committed snapshot")
    resume.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    resume.add_argument("--db", default=str(DEFAULT_DB))
    resume.add_argument("--config", default=None)
    resume.add_argument("--json", action="store_true")
    resume.set_defaults(
        func=_command_run,
        force_new=False,
        inject=[],
        calibration_update=[],
        crash_stage=None,
        crash_after_layers=0,
        crash_marker=None,
    )

    state = sub.add_parser("state", help="print current stage/layer/budget/error state")
    state.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    state.add_argument("--db", default=str(DEFAULT_DB))
    state.add_argument("--config", default=None)
    state.set_defaults(func=_command_state)

    verify = sub.add_parser("verify", help="run cross-module consistency checks")
    verify.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    verify.add_argument("--db", default=str(DEFAULT_DB))
    verify.add_argument("--config", default=None)
    verify.set_defaults(func=_command_verify)

    revision = sub.add_parser("revision", help="generate and optionally apply a weight revision")
    revision.add_argument("--base-bundle", default=str(DEFAULT_BUNDLE))
    revision.add_argument("--out", default=str(PROJECT_ROOT / "artifacts" / "bundle_next"))
    revision.add_argument("--layer", required=True)
    revision.add_argument("--revision", type=int, required=True)
    revision.add_argument("--db", default=str(DEFAULT_DB))
    revision.add_argument("--config", default=None)
    revision.add_argument("--run", action="store_true")
    revision.set_defaults(func=_command_revision)

    calibrate = sub.add_parser("calibrate", help="apply a calibration statistics update")
    calibrate.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    calibrate.add_argument("--db", default=str(DEFAULT_DB))
    calibrate.add_argument("--layer", required=True)
    calibrate.add_argument("--revision", type=int, required=True)
    calibrate.add_argument("--seed", type=int, default=None)
    calibrate.add_argument("--config", default=None)
    calibrate.set_defaults(func=_command_calibrate)

    demo = sub.add_parser("demo", help="run the repeatable crash/recovery/revision demo")
    demo.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    demo.add_argument("--work-dir", default=str(PROJECT_ROOT / "artifacts" / "demo"))
    demo.add_argument("--config", default=None)
    demo.set_defaults(func=_command_demo)

    inject = sub.add_parser("inject", help="run one input-triggered fault injection")
    inject.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    inject.add_argument("--db", default=str(PROJECT_ROOT / "artifacts" / "inject.sqlite"))
    inject.add_argument("--config", default=None)
    inject.add_argument("--stage", required=True)
    inject.add_argument("--kind", required=True)
    inject.add_argument("--layer", default=None)
    inject.set_defaults(func=_command_inject)

    serve = sub.add_parser("serve", help="start the local interactive web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    serve.add_argument("--db", default=str(PROJECT_ROOT / "artifacts" / "app.sqlite"))
    serve.add_argument("--config", default=None)
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=_command_serve)

    trace = sub.add_parser("trace", help="print atomic snapshot trace")
    trace.add_argument("--db", default=str(DEFAULT_DB))
    trace.add_argument("--limit", type=int, default=100)
    trace.set_defaults(func=_command_trace)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DeployError as error:
        _json_dump({"ok": False, "error": error.to_record()})
        return 2
    except (FileExistsError, ValueError, KeyError) as error:
        _json_dump(
            {
                "ok": False,
                "error": {
                    "code": type(error).__name__,
                    "message": str(error),
                },
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

