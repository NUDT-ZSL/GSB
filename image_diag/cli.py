from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .cache import CacheHooks
from .errors import BusinessError
from .service import ImageDiagService
from .state import StageGuard


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _run_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_key": result["run_key"],
        "precision": result["precision"],
        "focus_path": result["focus_path"],
        "whole_final_sha256": result["whole"]["final_sha256"],
        "block_final_sha256": result["block"]["final_sha256"],
        "block_match": result["block_match"],
        "dominant_source": result["diagnostics"]["dominant_source"],
        "final_max_abs": result["diagnostics"]["cascade"][-1]["abs_max"],
        "within_tolerance": result["diagnostics"]["within_tolerance"],
        "error": result["error"],
        "cache_events": result["cache_events"],
        "snapshot": result["snapshot"],
    }


def _fresh_workspace(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def command_generate(args: argparse.Namespace) -> int:
    from generate_sample import write_sample

    manifest = write_sample(Path(args.out))
    _print_json(
        {
            "out": str(Path(args.out).resolve()),
            "seed": manifest["seed"],
            "base_sha256": manifest["base_sha256"],
            "mask_sha256": manifest["mask_sha256"],
            "style_sha256": manifest["style_sha256"],
        }
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    service = ImageDiagService(args.workspace, args.sample_dir)
    result = service.run_compare(
        precision=args.precision,
        focus_path=args.path,
        policy=args.policy,
        threshold_tolerance=args.threshold_tolerance,
        error_tolerance=args.tolerance,
    )
    _print_json(result if args.full else _run_summary(result))
    return 2 if result["error"] else 0


def command_demo(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    if args.fresh:
        _fresh_workspace(workspace)
    service = ImageDiagService(workspace, args.sample_dir)
    output: dict[str, Any] = {"ok": True, "scenario_request": args.scenario}
    if args.scenario in ("incremental", "all"):
        output["incremental"] = service.demo_incremental()
    if args.scenario in ("style", "all"):
        output["style_revision"] = service.demo_style_revision()
    if args.scenario in ("rollback", "all"):
        output["rollback"] = service.demo_rollback()
    if args.scenario in ("busy", "all"):
        output["busy"] = service.demo_busy()
    if args.scenario in ("recovery", "all"):
        output["recovery"] = service.demo_recovery()
    output["snapshots"] = service.snapshot_state()
    output["cache"] = service.cache.summary()
    _print_json(output)
    return 0


def command_worker_run(args: argparse.Namespace) -> int:
    service = ImageDiagService(args.workspace, args.sample_dir)
    run_key = service.run_key_for(
        service.sample,
        precision=args.precision,
        focus_path="block",
        tolerance=service.sample.style.error_tolerance,
    )
    hooks = CacheHooks(
        service.cache,
        service.checkpoints,
        run_key=run_key,
        fail_after=args.fail_after,
        force_recompute=args.force_recompute,
    )
    result = service.run_compare(
        precision=args.precision,
        focus_path="block",
        hooks=hooks,
        commit_result=False,
    )
    _print_json(_run_summary(result))
    return 0


def command_lock_probe(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    try:
        with StageGuard(workspace / "locks", args.stage):
            _print_json({"ok": True, "acquired": True, "stage": args.stage})
    except BusinessError as exc:
        _print_json(exc.as_dict())
    return 0


def command_state(args: argparse.Namespace) -> int:
    service = ImageDiagService(args.workspace, args.sample_dir)
    _print_json(service.state_payload())
    return 0


def command_reset(args: argparse.Namespace) -> int:
    service = ImageDiagService(args.workspace, args.sample_dir)
    _print_json(service.reset_runtime_to_sample())
    return 0


def command_verify(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent.parent
    if args.workspace:
        workspace = Path(args.workspace)
        if args.fresh:
            _fresh_workspace(workspace)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="image-diag-verify-")
        workspace = Path(temporary.name)
    service = ImageDiagService(workspace, args.sample_dir)
    checks: dict[str, Any] = {}

    precision_runs: dict[str, dict[str, Any]] = {}
    decision_hashes: dict[str, dict[str, str]] = {}
    for precision in ("float64", "float32", "fixed"):
        result = service.run_compare(precision=precision)
        precision_runs[precision] = {
            "whole_sha256": result["whole"]["final_sha256"],
            "block_sha256": result["block"]["final_sha256"],
            "bitwise_equal": result["block_match"]["bitwise_equal"],
            "dominant_source": result["diagnostics"]["dominant_source"],
            "final_max_abs": result["diagnostics"]["cascade"][-1]["abs_max"],
            "within_tolerance": result["diagnostics"]["within_tolerance"],
            "error": result["error"]["code"] if result["error"] else None,
        }
        control = service._last_execution[0]
        decision_hashes[precision] = {
            "style": control.threshold_audit.style_decision_hash,
            "boundary": control.threshold_audit.boundary_decision_hash,
        }
    checks["dual_paths"] = precision_runs
    checks["threshold_decisions_stable"] = len(
        {tuple(sorted(value.items())) for value in decision_hashes.values()}
    ) == 1
    checks["float64_bitwise"] = all(
        item["bitwise_equal"] for item in precision_runs.values()
    )

    first = service.run_compare(precision="float32")
    second = service.run_compare(precision="float32")
    checks["deterministic_repeat"] = {
        "stage_hashes_equal": first["block"]["stage_records"]
        == second["block"]["stage_records"],
        "final_equal": first["block"]["final_sha256"]
        == second["block"]["final_sha256"],
    }

    incremental = service.demo_incremental()
    checks["incremental"] = {
        "recomputed_regions": incremental["recomputed_regions"],
        "reused_regions": incremental["reused_regions"],
        "only_r2_recomputed": incremental["recomputed_regions"] == ["R2"],
        "equal_full_recompute": incremental["incremental_equal_full"],
    }
    style_revision = service.demo_style_revision()
    checks["style_revision"] = {
        "upstream_reused": style_revision["upstream_reused"],
        "downstream_recomputed": style_revision["downstream_recomputed"],
        "equal_full_recompute": style_revision["revision_equal_full"],
        "cache_events": style_revision["cache_events"],
    }
    rollback = service.demo_rollback()
    checks["rollback"] = {
        "code": rollback["error"]["code"],
        "head_unchanged": rollback["head_unchanged"],
    }
    busy = service.demo_busy()
    checks["stage_busy"] = {
        "code": busy["error"]["code"],
        "cross_process": busy["cross_process"],
    }
    recovery = service.demo_recovery()
    checks["recovery"] = {
        "return_code": recovery["terminated_process_return_code"],
        "checkpoint_count_before_resume": recovery["checkpoint_count_before_resume"],
        "resumed_checkpoint_events": recovery["resumed_checkpoint_events"],
        "equal_full_recompute": recovery["recovery_equal_full"],
    }
    passed = bool(
        all(item["bitwise_equal"] for item in precision_runs.values())
        and checks["threshold_decisions_stable"]
        and checks["deterministic_repeat"]["stage_hashes_equal"]
        and checks["deterministic_repeat"]["final_equal"]
        and checks["incremental"]["only_r2_recomputed"]
        and checks["incremental"]["equal_full_recompute"]
        and checks["style_revision"]["upstream_reused"]
        and checks["style_revision"]["downstream_recomputed"]
        and checks["style_revision"]["equal_full_recompute"]
        and checks["rollback"]["code"] == "E_SNAPSHOT_ROLLBACK"
        and checks["rollback"]["head_unchanged"]
        and checks["stage_busy"]["code"] == "E_STAGE_BUSY"
        and checks["recovery"]["return_code"] == 42
        and checks["recovery"]["resumed_checkpoint_events"] >= 6
        and checks["recovery"]["equal_full_recompute"]
    )
    _print_json(
        {
            "ok": passed,
            "workspace": str(workspace),
            "sample_dir": str(Path(args.sample_dir).resolve()),
            "checks": checks,
            "snapshots": service.snapshot_state(),
            "cache": service.cache.summary(),
        }
    )
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m image_diag",
        description="Deterministic image repair precision diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate the fixed-seed sample")
    generate.add_argument("--out", default="data/sample")
    generate.set_defaults(func=command_generate)

    run = subparsers.add_parser("run", help="Run dual-path precision comparison")
    run.add_argument("--workspace", default="data/runtime")
    run.add_argument("--sample-dir", default="data/sample")
    run.add_argument("--precision", choices=("float64", "float32", "fixed"), default="float64")
    run.add_argument("--path", choices=("whole", "block"), default="block")
    run.add_argument("--policy", choices=("strict", "tolerance_band"))
    run.add_argument("--threshold-tolerance", type=float)
    run.add_argument("--tolerance", type=float)
    run.add_argument("--full", action="store_true")
    run.set_defaults(func=command_run)

    demo = subparsers.add_parser("demo", help="Run deterministic demonstrations")
    demo.add_argument(
        "--scenario",
        choices=("incremental", "style", "rollback", "busy", "recovery", "all"),
        default="all",
    )
    demo.add_argument("--workspace", default="data/runtime")
    demo.add_argument("--sample-dir", default="data/sample")
    demo.add_argument("--fresh", action="store_true")
    demo.set_defaults(func=command_demo)

    serve = subparsers.add_parser("serve", help="Start the local web dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--workspace", default="data/runtime")
    serve.add_argument("--sample-dir", default="data/sample")
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(func=command_serve)

    worker = subparsers.add_parser("worker-run", help=argparse.SUPPRESS)
    worker.add_argument("--workspace", required=True)
    worker.add_argument("--sample-dir", default="data/sample")
    worker.add_argument("--precision", default="float64")
    worker.add_argument("--fail-after", type=int)
    worker.add_argument("--force-recompute", action="store_true")
    worker.set_defaults(func=command_worker_run)

    lock_probe = subparsers.add_parser("lock-probe", help=argparse.SUPPRESS)
    lock_probe.add_argument("--workspace", required=True)
    lock_probe.add_argument("--stage", required=True)
    lock_probe.set_defaults(func=command_lock_probe)

    state = subparsers.add_parser("state", help="Print current service state")
    state.add_argument("--workspace", default="data/runtime")
    state.add_argument("--sample-dir", default="data/sample")
    state.set_defaults(func=command_state)

    reset = subparsers.add_parser("reset", help="Restore the canonical sample into the runtime")
    reset.add_argument("--workspace", default="data/runtime")
    reset.add_argument("--sample-dir", default="data/sample")
    reset.set_defaults(func=command_reset)

    verify = subparsers.add_parser("verify", help="Run the complete acceptance verification")
    verify.add_argument("--workspace")
    verify.add_argument("--sample-dir", default="data/sample")
    verify.add_argument("--fresh", action="store_true")
    verify.set_defaults(func=command_verify)
    return parser


def command_serve(args: argparse.Namespace) -> int:
    from .web import serve

    return serve(
        host=args.host,
        port=args.port,
        workspace=args.workspace,
        sample_dir=args.sample_dir,
        open_browser=not args.no_browser,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        _print_json(exc.as_dict())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
