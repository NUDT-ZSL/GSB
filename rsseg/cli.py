from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from threading import Event
from typing import Any

import numpy as np

from .engine import PipelineEngine
from .errors import InputError
from .sample import generate_sample
from .sequence import run_edit_sequence
from .state import StateStore


def _add_common_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default="sample")
    parser.add_argument("--state", required=True)
    parser.add_argument("--config")
    parser.add_argument("--index")
    parser.add_argument("--overlay")
    parser.add_argument("--max-batches", type=int, default=1, help="0 means run until converged")


def _print_result(result: Any) -> None:
    summary = {
        "status": result.report.get("status"),
        "converged": result.converged,
        "business_codes": result.business_codes,
        "snapshot_version": result.snapshot_version,
        "snapshot_manifest_sha256": result.report.get("snapshot_manifest_sha256"),
        "tile_counts": result.report.get("tile_counts"),
        "unconverged": result.report.get("unconverged"),
        "area_stats": result.report.get("area_stats"),
        "failures": result.report.get("failures"),
        "timings_ms": result.report.get("timings_ms"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _verify_states(left: str | Path, right: str | Path) -> bool:
    left_snapshot = StateStore(left).load_snapshot()
    right_snapshot = StateStore(right).load_snapshot()
    if left_snapshot is None or right_snapshot is None:
        raise RuntimeError("one or both snapshots failed manifest/checksum validation")
    category_equal = np.array_equal(left_snapshot.category, right_snapshot.category)
    score_equal = True
    left_keys = {key for key in left_snapshot.tile_results if key.startswith("scores_")}
    right_keys = {key for key in right_snapshot.tile_results if key.startswith("scores_")}
    if left_keys != right_keys:
        score_equal = False
    else:
        for key in left_keys:
            if not np.array_equal(left_snapshot.tile_results[key], right_snapshot.tile_results[key]):
                score_equal = False
                break
    print(
        json.dumps(
            {
                "category_equal": bool(category_equal),
                "tile_scores_equal": bool(score_equal),
                "left_version": left_snapshot.version,
                "right_version": right_snapshot.version,
            },
            indent=2,
        )
    )
    return bool(category_equal and score_equal)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rsseg", description="Deterministic incremental RS tile segmentation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed local sample")
    generate.add_argument("--out", default="sample")
    generate.add_argument("--seed", type=int, default=20261117)

    run = subparsers.add_parser("run", help="run one or more incremental batches")
    _add_common_run_args(run)

    full = subparsers.add_parser("full", help="run from scratch ignoring the local budget")
    _add_common_run_args(full)

    verify = subparsers.add_parser("verify", help="compare category and tile scores in two snapshots")
    verify.add_argument("--left", required=True)
    verify.add_argument("--right", required=True)

    sequence = subparsers.add_parser("sequence", help="apply edit sequence and compare every final state with full mode")
    sequence.add_argument("--workspace", default="sample")
    sequence.add_argument("--state", default="states/sequence")
    sequence.add_argument("--edits", default="sample/edits.json")
    sequence.add_argument("--report", default="artifacts/sequence_report.json")
    sequence.add_argument("--max-batches-per-edit", type=int, default=100)
    sequence.add_argument("--keep-state", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            print(json.dumps(generate_sample(args.out, args.seed), ensure_ascii=False, indent=2))
            return 0
        if args.command == "verify":
            return 0 if _verify_states(args.left, args.right) else 1
        if args.command == "sequence":
            report = run_edit_sequence(
                args.workspace,
                args.state,
                args.edits,
                report_path=args.report,
                max_batches_per_edit=args.max_batches_per_edit,
                reset_state=not args.keep_state,
            )
            print(json.dumps({"all_equal": report["all_equal"], "report": args.report}, indent=2))
            return 0 if report["all_equal"] else 1

        stop_event = Event()

        def handle_signal(signum: int, frame: Any) -> None:
            stop_event.set()

        previous_int = signal.signal(signal.SIGINT, handle_signal)
        previous_term = signal.signal(signal.SIGTERM, handle_signal)
        try:
            result = PipelineEngine(
                args.workspace,
                args.state,
                config_path=args.config,
                index_path=args.index,
                overlay_path=args.overlay,
                mode="full" if args.command == "full" else "incremental",
                max_batches=0 if args.max_batches == 0 else args.max_batches,
                stop_event=stop_event,
            ).run()
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        _print_result(result)
        return result.exit_code
    except InputError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": exc.code,
                    "stage": exc.stage,
                    "message": exc.message,
                    "row": exc.row,
                    "col": exc.col,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "message": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
