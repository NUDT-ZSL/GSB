"""Command line and small interactive console for the qpipe application.

Run ``python -m qpipe.cli --help``.  The interactive menu never reaches out to
the network and only operates on the locally generated deterministic sample.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

import numpy as np

from . import samplegen
from .consistency import run_all_checks
from .errors import PipelineError
from .models import ResourceBudget, RunContext
from .pipeline import QuantPipeline
from .storage import ProgressStore, WorkDir


def _print_json(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False))


def generate_cmd(args: argparse.Namespace) -> None:
    descriptor = samplegen.generate_workspace(
        args.data_dir,
        seed=args.seed,
        depth=args.depth,
        calibration_split=args.split,
    )
    print(f"generated deterministic sample in {args.data_dir}")
    _print_json(
        {
            "manifest": descriptor["manifest"],
            "layer_count": descriptor["layer_count"],
            "seed": descriptor["seed"],
        }
    )


def _make_pipeline(args: argparse.Namespace, context: Optional[RunContext] = None):
    budget = ResourceBudget()
    if args.budget:
        with open(args.budget, "r", encoding="utf-8") as handle:
            budget = ResourceBudget.from_dict(json.load(handle))
    return QuantPipeline(
        args.data_dir,
        args.work_dir,
        budget=budget,
        context=context or RunContext(seed=args.seed),
    )


def run_cmd(args: argparse.Namespace) -> None:
    context = _load_context(args)
    pipeline = _make_pipeline(args, context)
    summary = pipeline.run_all()
    _emit_summary(args, pipeline, summary)


def resume_cmd(args: argparse.Namespace) -> None:
    pipeline = _make_pipeline(args, RunContext(seed=args.seed))
    summary = pipeline.resume()
    _emit_summary(args, pipeline, summary)


def _load_context(args: argparse.Namespace) -> RunContext:
    data: Dict[str, Any] = {"seed": args.seed}
    if args.faults:
        with open(args.faults, "r", encoding="utf-8") as handle:
            data["faults"] = json.load(handle)
    if args.kill_after_layer is not None:
        data["kill_after_layer"] = args.kill_after_layer
        data["kill_stage"] = args.kill_stage or "infer"
    return RunContext.from_dict(data)


def _emit_summary(
    args: argparse.Namespace,
    pipeline: QuantPipeline,
    summary,
) -> None:
    payload = summary.to_dict()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
        print(f"summary written to {args.out}")
    else:
        _print_json(payload)


def status_cmd(args: argparse.Namespace) -> None:
    work = WorkDir(args.work_dir)
    store = ProgressStore(work)
    head = store.head()
    if head is None:
        print("no committed progress point yet")
        return
    print(
        f"stage={head['stage']} progress_version={head['progress_version']} "
        f"completed={head['completed_layers']} degraded={head.get('degraded')}"
    )
    print(f"streaming={head.get('streaming')} history={len(store.history())}")


def check_cmd(args: argparse.Namespace) -> None:
    pipeline = _make_pipeline(args)
    package = samplegen.load_manifest(pipeline._manifest_path)
    calibration = samplegen.load_calibration(pipeline._calibration_path)
    report = run_all_checks(
        package, pipeline.store, pipeline.cache, calibration
    )
    _print_json(report)


def kill_demo_cmd(args: argparse.Namespace) -> None:
    """Spawn a child that hard-exits mid-stage, then resume and compare."""
    baseline_dir = args.work_dir + "_baseline"
    kill_dir = args.work_dir
    for directory in (baseline_dir, kill_dir):
        if os.path.exists(directory):
            shutil.rmtree(directory)

    baseline = QuantPipeline(
        args.data_dir, baseline_dir, context=RunContext(seed=args.seed)
    )
    full_summary = baseline.run_all().to_dict()

    env = dict(os.environ)
    child_cmd = [
        sys.executable,
        "-m",
        "qpipe.cli",
        "--data-dir",
        args.data_dir,
        "--work-dir",
        kill_dir,
        "--seed",
        str(args.seed),
        "run",
        "--kill-after-layer",
        str(args.kill_after_layer),
        "--kill-stage",
        args.kill_stage,
    ]
    print(f"spawning child that will hard-exit: {' '.join(child_cmd)}")
    killed = subprocess.run(child_cmd, env=env, capture_output=True, text=True)
    print(f"child exit code={killed.returncode} (no exit report is produced)")

    interrupted_head = ProgressStore(WorkDir(kill_dir)).head()
    resumed = QuantPipeline(
        args.data_dir, kill_dir, context=RunContext(seed=args.seed)
    )
    resume_summary = resumed.resume().to_dict()

    same_prediction = (
        resume_summary["prediction_checksum"]
        == full_summary["prediction_checksum"]
    )
    same_activations = (
        resume_summary["activation_checksums"]
        == full_summary["activation_checksums"]
    )
    report = {
        "child_exit_code": killed.returncode,
        "interrupted_head_stage": (
            interrupted_head["stage"] if interrupted_head else None
        ),
        "interrupted_head_version": (
            interrupted_head["progress_version"] if interrupted_head else 0
        ),
        "prediction_byte_identical": same_prediction,
        "activation_byte_identical": same_activations,
        "full_prediction_checksum": full_summary["prediction_checksum"],
        "resume_prediction_checksum": resume_summary["prediction_checksum"],
    }
    _print_json(report)
    if not (same_prediction and same_activations):
        raise SystemExit(2)


def revision_demo_cmd(args: argparse.Namespace) -> None:
    """Bump one layer's revision and verify downstream-only recomputation."""
    if os.path.exists(args.work_dir):
        shutil.rmtree(args.work_dir)
    pipeline = QuantPipeline(
        args.data_dir, args.work_dir, context=RunContext(seed=args.seed)
    )
    before = pipeline.run_all().to_dict()

    package = samplegen.load_manifest(pipeline._manifest_path)
    changed = args.change_layer
    specs = list(package.layers)
    new_specs = []
    new_weights: Dict[int, np.ndarray] = {}
    for spec in specs:
        if spec.layer_index == changed:
            from dataclasses import replace

            spec = replace(spec, weight_revision=spec.weight_revision + 1)
            weight = samplegen.generate_weight(spec, seed=args.seed)
            new_weights[changed] = weight
        new_specs.append(spec)
    new_package = type(package)(
        model_tag=package.model_tag, layers=tuple(new_specs)
    )
    after = pipeline.apply_revision(
        new_package,
        changed_layer=changed,
        new_weights=new_weights,
    ).to_dict()

    expected_affected = new_package.downstream_of(changed)
    report = {
        "changed_layer": changed,
        "expected_affected": expected_affected,
        "recomputed_layers": after["recomputed_layers"],
        "reused_layers": after["reused_layers"],
        "only_affected_recomputed": sorted(
            after["recomputed_layers"]
        )
        == sorted(expected_affected),
        "prediction_changed": (
            after["prediction_checksum"] != before["prediction_checksum"]
        ),
        "head_version": after["head_version"],
    }
    _print_json(report)


def interactive_cmd(args: argparse.Namespace) -> None:
    menu = (
        "\nqpipe interactive console\n"
        " 1) show current stage / progress / errors\n"
        " 2) show per-layer quantisation error\n"
        " 3) run full pipeline\n"
        " 4) kill-and-resume demonstration\n"
        " 5) weight revision / invalidation demonstration\n"
        " 6) cross-module consistency self-check\n"
        " 7) regenerate deterministic sample\n"
        " q) quit\n"
    )
    while True:
        print(menu)
        choice = input("select> ").strip().lower()
        try:
            if choice == "1":
                status_cmd(args)
            elif choice == "2":
                _show_quant_errors(args)
            elif choice == "3":
                run_cmd(args)
            elif choice == "4":
                args.kill_after_layer = 1
                args.kill_stage = "infer"
                kill_demo_cmd(args)
            elif choice == "5":
                args.change_layer = 2
                revision_demo_cmd(args)
            elif choice == "6":
                check_cmd(args)
            elif choice == "7":
                generate_cmd(args)
            elif choice in ("q", "quit", "exit"):
                return
            else:
                print("unknown selection")
        except PipelineError as exc:
            print(f"business error: {exc}")
            _print_json(exc.to_dict())


def _show_quant_errors(args: argparse.Namespace) -> None:
    pipeline = _make_pipeline(args)
    package = samplegen.load_manifest(pipeline._manifest_path)
    rows = []
    for spec in package.layers:
        entry = pipeline.cache.read_entry("quantize", spec.layer_index)
        if entry:
            payload = entry["payload"]
            rows.append(
                {
                    "layer": payload["layer_id"],
                    "revision": payload["weight_revision"],
                    "max_abs_error": payload["max_abs_error"],
                    "rmse": payload["rmse"],
                    "snr_db": payload["snr_db"],
                }
            )
    if not rows:
        print("no quantisation cache yet - run the pipeline first")
    else:
        _print_json({"quant_errors": rows})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qpipe",
        description="Local NumPy quantisation/calibration/resumable inference.",
    )
    parser.add_argument("--data-dir", default="data/sample")
    parser.add_argument("--work-dir", default="data/work")
    parser.add_argument("--seed", type=int, default=samplegen.DEFAULT_SEED)
    parser.add_argument("--budget")
    parser.add_argument("--faults")
    parser.add_argument("--out")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="create deterministic sample")
    gen.add_argument("--depth", type=int, default=5)
    gen.add_argument("--split", type=int, default=4)
    gen.set_defaults(func=generate_cmd)

    run = sub.add_parser("run", help="run four stages")
    run.add_argument("--kill-after-layer", type=int)
    run.add_argument("--kill-stage", default="infer")
    run.set_defaults(func=run_cmd)

    resume = sub.add_parser("resume", help="resume from latest point")
    resume.set_defaults(func=resume_cmd)

    status = sub.add_parser("status", help="show HEAD progress point")
    status.set_defaults(func=status_cmd)

    check = sub.add_parser("check", help="consistency self-check")
    check.set_defaults(func=check_cmd)

    kill = sub.add_parser("kill-demo", help="forced termination + resume demo")
    kill.add_argument("--kill-after-layer", type=int, default=1)
    kill.add_argument("--kill-stage", default="infer")
    kill.set_defaults(func=kill_demo_cmd)

    rev = sub.add_parser("revision-demo", help="revision invalidation demo")
    rev.add_argument("--change-layer", type=int, default=2)
    rev.set_defaults(func=revision_demo_cmd)

    inter = sub.add_parser("interactive", help="interactive console")
    inter.set_defaults(func=interactive_cmd)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except PipelineError as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True, indent=2), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
