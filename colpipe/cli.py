"""Command line entry points for ColPipe."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .catalog import load_pipeline_input
from .codes import E_DIVERGE, E_INTERNAL
from .demo import generate_demo_files, run_demo, run_demo_main
from .errors import PipeError
from .executor import compare_results, execute
from .plan import build_plans
from .sample import sample_payload
from .snapshot import create_snapshot, load_snapshot, restore_snapshot, save_snapshot
from .util import write_bytes, write_json
from .verifier import verify_snapshot


def _as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _write_execution(output_dir: Path, execution) -> None:
    write_json(output_dir / f"{execution.mode}_result.json", execution.to_artifact())
    write_json(output_dir / f"{execution.mode}_trace.json", {"mode": execution.mode, "trace": execution.trace, "stats": execution.stats})
    write_bytes(output_dir / f"{execution.mode}.bin", execution.canonical_bytes)


def _cmd_generate(args: argparse.Namespace) -> int:
    root = Path(args.workspace).resolve()
    output = root / "artifacts" / "demo"
    generate_demo_files(output)
    result = {
        "status": "PASS",
        "business_code": "OK",
        "input": str(output / "pipeline_input.json"),
        "input_v2": str(output / "pipeline_input_v2.json"),
        "tables": [table["name"] for table in sample_payload()["tables"]],
    }
    print(_as_json(result))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    request = load_pipeline_input(args.input)
    bundle = build_plans(request)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        output_dir / "logical_plans.json",
        {
            "optimized": {
                "summary_sha256": bundle.optimized_summary_sha256,
                "summary": bundle.optimized_summary,
                "plan": bundle.optimized_plan,
            },
            "baseline": {
                "summary_sha256": bundle.baseline_summary_sha256,
                "summary": bundle.baseline_summary,
                "plan": bundle.baseline_plan,
            },
            "pushdown": bundle.pushdown,
            "pruning": bundle.pruning,
            "estimation": bundle.estimates,
            "split_plan": bundle.split_plan,
            "baseline_split_plan": bundle.baseline_split_plan,
        },
    )
    if args.mode == "compare":
        optimized = execute(bundle, mode="optimized")
        baseline = execute(bundle, mode="baseline")
        _write_execution(output_dir, optimized)
        _write_execution(output_dir, baseline)
        comparison = compare_results(optimized, baseline)
        snapshot = create_snapshot(bundle, {"optimized": optimized, "baseline": baseline}, comparison)
        save_snapshot(output_dir / "session_snapshot.json", snapshot)
        result = {
            "status": "PASS",
            "business_code": "OK",
            "mode": "compare",
            "comparison": comparison,
            "optimized": optimized.stats,
            "baseline": baseline.stats,
            "snapshot_id": snapshot["snapshot_id"],
            "output_dir": str(output_dir.resolve()),
        }
    else:
        execution = execute(bundle, mode=args.mode)
        _write_execution(output_dir, execution)
        result = {
            "status": "PASS",
            "business_code": "OK",
            "mode": args.mode,
            "stats": execution.stats,
            "result_sha256": execution.canonical_sha256,
            "output_dir": str(output_dir.resolve()),
        }
    write_json(output_dir / "run_report.json", result)
    print(_as_json(result))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    request = load_pipeline_input(args.input)
    bundle = build_plans(request)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    optimized = execute(bundle, mode="optimized")
    baseline = execute(bundle, mode="baseline")
    _write_execution(output_dir, optimized)
    _write_execution(output_dir, baseline)
    try:
        comparison = compare_results(optimized, baseline)
    except PipeError as exc:
        if exc.code != E_DIVERGE:
            raise
        write_json(output_dir / "divergence.json", exc.to_dict())
        print(_as_json(exc.to_dict()))
        return 3
    snapshot = create_snapshot(bundle, {"optimized": optimized, "baseline": baseline}, comparison)
    save_snapshot(output_dir / "session_snapshot.json", snapshot)
    result = {
        "status": "PASS",
        "business_code": "OK",
        "comparison": comparison,
        "snapshot_id": snapshot["snapshot_id"],
        "optimized": optimized.stats,
        "baseline": baseline.stats,
        "output_dir": str(output_dir.resolve()),
    }
    write_json(output_dir / "comparison_report.json", result)
    print(_as_json(result))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    result = verify_snapshot(
        args.snapshot,
        output_path=args.output,
        artifacts_dir=args.artifacts,
    )
    if args.output is None:
        print(_as_json(result))
    else:
        print(
            _as_json(
                {
                    "status": result["status"],
                    "code": result["code"],
                    "summary": result["summary"],
                    "output": str(Path(args.output).resolve()),
                }
            )
        )
    return 0 if result["status"] == "PASS" else 4


def _cmd_restore(args: argparse.Namespace) -> int:
    snapshot = load_snapshot(args.snapshot)
    current = load_pipeline_input(args.current)
    restored = restore_snapshot(snapshot, current)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "restore_report.json", restored.report)
    write_json(output_dir / "restored_input.json", restored.request.input_dict())
    bundle = build_plans(restored.request)
    replay = execute(bundle, mode="optimized")
    _write_execution(output_dir, replay)
    expected_hash = snapshot.get("executions", {}).get("optimized", {}).get("result", {}).get(
        "canonical_sha256"
    )
    equal = replay.canonical_sha256 == expected_hash
    stats_ok = restored.statistics.get("statistics_sha256") == bundle.statistics.get("statistics_sha256")
    result = {
        "status": "PASS" if equal and stats_ok else "FAIL",
        "business_code": "OK" if equal and stats_ok else E_DIVERGE,
        "mode": "restore_replay",
        "restore_report": restored.report,
        "expected_snapshot_result_sha256": expected_hash,
        "replay_result_sha256": replay.canonical_sha256,
        "byte_equal": equal,
        "statistics_rebuilt": stats_ok,
        "output_dir": str(output_dir.resolve()),
    }
    write_json(output_dir / "restore_replay_report.json", result)
    print(_as_json(result))
    return 0 if result["status"] == "PASS" else 4


def _cmd_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui

    return run_gui(args.input, workspace=args.workspace)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m colpipe",
        description="Deterministic columnar pushdown/pruning pipeline for local Windows environments.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="write the sample and run all three scenarios")
    demo.add_argument("--workspace", default=".", help="workspace directory (default: current directory)")
    demo.set_defaults(func=lambda args: run_demo_main(args.workspace))

    generate = subparsers.add_parser("generate", help="generate deterministic sample JSON files")
    generate.add_argument("--workspace", default=".", help="workspace directory")
    generate.set_defaults(func=_cmd_generate)

    run = subparsers.add_parser("run", help="compile and execute one input")
    run.add_argument("--input", required=True, help="pipeline input JSON")
    run.add_argument("--mode", choices=("optimized", "baseline", "compare"), default="compare")
    run.add_argument("--output-dir", default="artifacts/run", help="artifact output directory")
    run.set_defaults(func=_cmd_run)

    compare = subparsers.add_parser("compare", help="execute both paths and verify byte equality")
    compare.add_argument("--input", required=True, help="pipeline input JSON")
    compare.add_argument("--output-dir", default="artifacts/compare", help="artifact output directory")
    compare.set_defaults(func=_cmd_compare)

    verify = subparsers.add_parser("verify", help="independently verify a session snapshot")
    verify.add_argument("--snapshot", required=True, help="snapshot JSON path")
    verify.add_argument("--output", default="artifacts/verification.json", help="verification report output")
    verify.add_argument("--artifacts", default=None, help="optional result/trace artifact directory")
    verify.set_defaults(func=_cmd_verify)

    restore = subparsers.add_parser("restore", help="whole-state restore from a snapshot and replay optimized execution")
    restore.add_argument("--snapshot", required=True, help="snapshot JSON path")
    restore.add_argument("--current", required=True, help="current pipeline input JSON")
    restore.add_argument("--output-dir", default="artifacts/restore", help="restore/replay output directory")
    restore.set_defaults(func=_cmd_restore)

    gui = subparsers.add_parser("gui", help="launch the Tkinter interface")
    gui.add_argument("--input", default=None, help="pipeline input JSON; defaults to the demo sample")
    gui.add_argument("--workspace", default=".", help="workspace directory")
    gui.set_defaults(func=_cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PipeError as exc:
        try:
            output_dir = getattr(args, "output_dir", None)
            if output_dir:
                write_json(Path(output_dir) / "error.json", exc.to_dict())
        except Exception:
            pass
        print(_as_json(exc.to_dict()))
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        error = PipeError(E_INTERNAL, f"unexpected internal error: {exc}", "$")
        print(_as_json(error.to_dict()))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
