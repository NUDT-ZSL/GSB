"""Command-line interface for the local reconstruction workbench."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import traceback
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, read_json
from .injection import load_injections
from .pipeline import ReconstructionWorkbench
from .samples import generate_sample_dataset, materialize_injections


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m recon_workbench",
        description="Deterministic, resumable multi-view 3D reconstruction workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate a deterministic local sample")
    generate.add_argument("--out", type=Path, required=True)
    generate.add_argument("--views", type=int, default=8)
    generate.add_argument("--seed", type=int, default=20261117)
    generate.add_argument("--width", type=int, default=640)
    generate.add_argument("--height", type=int, default=480)
    generate.add_argument("--markers", type=int, default=72)

    materialize = subparsers.add_parser(
        "materialize-injections",
        help="copy a sample and apply file-level injected corruptions",
    )
    materialize.add_argument("--input", type=Path, required=True)
    materialize.add_argument("--out", type=Path, required=True)
    materialize.add_argument("--inject", type=Path, required=True)
    materialize.add_argument("--profile", default="recoverable")

    run = subparsers.add_parser("run", help="run or resume reconstruction")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--inject", type=Path)
    run.add_argument("--inject-profile", default="recoverable")
    run.add_argument("--state", type=Path, default=Path("workbench_state"))
    run.add_argument("--cache", type=Path)
    run.add_argument("--report", type=Path)
    run.add_argument("--cloud-out", type=Path)
    run.add_argument("--depth-out", type=Path)
    run.add_argument("--features-out", type=Path)
    run.add_argument("--matches-out", type=Path)
    run.add_argument("--fresh", action="store_true", help="ignore existing snapshots")
    run.add_argument("--no-cache", action="store_true", help="disable artifact reuse")
    run.add_argument(
        "--test-delay-ms",
        type=int,
        default=0,
        help=argparse.SUPPRESS,
    )

    compare = subparsers.add_parser("compare-reports", help="compare deterministic result sections")
    compare.add_argument("--left", type=Path, required=True)
    compare.add_argument("--right", type=Path, required=True)
    compare.add_argument("--out", type=Path)
    return parser


def _install_signal_handlers(workbench: ReconstructionWorkbench) -> list[tuple[int, Any]]:
    installed: list[tuple[int, Any]] = []

    def handler(signum: int, frame: Any) -> None:
        del signum, frame
        workbench.request_stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        candidate = getattr(signal, name, None)
        if candidate is None:
            continue
        try:
            previous = signal.getsignal(candidate)
            signal.signal(candidate, handler)
            installed.append((candidate, previous))
        except (OSError, ValueError):
            continue
    return installed


def _restore_signal_handlers(installed: list[tuple[int, Any]]) -> None:
    for signum, previous in installed:
        try:
            signal.signal(signum, previous)
        except (OSError, ValueError):
            pass


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def _run_command(args: argparse.Namespace) -> int:
    workbench = ReconstructionWorkbench(
        input_dir=args.input,
        config_path=args.config,
        injection_path=args.inject,
        injection_profile=args.inject_profile if args.inject else None,
        state_root=args.state,
        cache_root=args.cache,
        report_path=args.report,
        cloud_out_path=args.cloud_out,
        depth_out_path=args.depth_out,
        features_out_path=args.features_out,
        matches_out_path=args.matches_out,
        resume=not args.fresh,
        cache_enabled=not args.no_cache,
        test_delay_ms=args.test_delay_ms,
    )
    installed = _install_signal_handlers(workbench)
    try:
        outcome = workbench.run()
    except KeyboardInterrupt:
        workbench.request_stop()
        outcome = workbench.run()
    finally:
        _restore_signal_handlers(installed)
    result = outcome.report.get("result", {})
    summary = {
        "status": outcome.status,
        "exit_code": outcome.exit_code,
        "report": str(workbench.report_path),
        "cloud": str(workbench.cloud_out_path) if outcome.cloud is not None else None,
        "recovery_point": outcome.report.get("run", {}).get("recovery_point"),
        "latest_snapshot_sequence": outcome.report.get("run", {}).get(
            "latest_snapshot_sequence"
        ),
        "business_codes": result.get("business_codes", []),
        "reconstructed_point_count": result.get("reconstructed_point_count", 0),
        "voxel_count": result.get("voxel_count", 0),
        "result_digest": result.get("result_digest"),
    }
    _print_json(summary)
    return int(outcome.exit_code)


def _compare_reports(left_path: Path, right_path: Path) -> dict[str, Any]:
    left = read_json(left_path)
    right = read_json(right_path)
    left_result = left.get("result", {})
    right_result = right.get("result", {})
    equal = left_result == right_result
    differences: list[str] = []
    if not equal:
        all_keys = sorted(set(left_result) | set(right_result))
        for key in all_keys:
            if left_result.get(key) != right_result.get(key):
                differences.append(key)
    return {
        "left": str(left_path),
        "right": str(right_path),
        "result_equal": equal,
        "result_digest_left": left_result.get("result_digest"),
        "result_digest_right": right_result.get("result_digest"),
        "differing_result_fields": differences,
        "cloud_digest_equal": left_result.get("cloud_digest") == right_result.get("cloud_digest"),
        "point_count_equal": left_result.get("reconstructed_point_count")
        == right_result.get("reconstructed_point_count"),
        "voxel_count_equal": left_result.get("voxel_count") == right_result.get("voxel_count"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        manifest = generate_sample_dataset(
            args.out,
            seed=args.seed,
            view_count=args.views,
            width=args.width,
            height=args.height,
            marker_count=args.markers,
        )
        _print_json(
            {
                "status": "generated",
                "out": str(args.out),
                "view_count": manifest["view_count"],
                "width": manifest["width"],
                "height": manifest["height"],
                "manifest": str(args.out / "sample_manifest.json"),
            }
        )
        return 0
    if args.command == "materialize-injections":
        injections = load_injections(args.inject, args.profile)
        materialize_injections(args.input, args.out, injections)
        _print_json(
            {
                "status": "materialized",
                "out": str(args.out),
                "profile": args.profile,
                "injection_ids": [item.id for item in injections.items],
            }
        )
        return 0
    if args.command == "run":
        try:
            return _run_command(args)
        except Exception as error:
            report_path = args.report or (args.state / "report.json")
            error_report = {
                "schema_version": 1,
                "status": "internal_error",
                "exit_code": 4,
                "result": {
                    "complete": False,
                    "business_codes": ["E_INTERNAL"],
                    "failures": [
                        {
                            "code": "E_INTERNAL",
                            "stage": "internal",
                            "message": type(error).__name__ + ": " + str(error),
                        }
                    ],
                },
                "traceback": traceback.format_exc(),
            }
            atomic_write_json(report_path, error_report)
            _print_json(
                {
                    "status": "internal_error",
                    "exit_code": 4,
                    "message": type(error).__name__ + ": " + str(error),
                    "report": str(report_path),
                }
            )
            return 4
    if args.command == "compare-reports":
        comparison = _compare_reports(args.left, args.right)
        if args.out is not None:
            atomic_write_json(args.out, comparison)
        _print_json(comparison)
        return 0 if comparison["result_equal"] else 1
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
