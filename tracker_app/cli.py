from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import load_config, resolve_paths
from .data_gen import generate_dataset, mutate_detection, write_dataset
from .engine import TrackingEngine
from .errors import TrackingError
from .jsonio import (
    atomic_write_json,
    atomic_write_jsonl,
    pretty_json,
    read_json,
    read_jsonl,
    sha256_bytes,
)
from .validation import preprocess_dataset


COMPARABLE_OUTPUTS = (
    "assignments.jsonl",
    "match_candidates.jsonl",
    "track_states.jsonl",
    "gap_completions.jsonl",
    "gap_intervals.json",
    "gap_skipped.json",
    "segments.json",
    "metrics.json",
)


def _base_dir(args: argparse.Namespace) -> Path:
    return Path(args.base_dir).resolve()


def _load_dataset(data_dir: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    root = Path(data_dir)
    detections = read_jsonl(root / "detections.jsonl")
    truth = read_jsonl(root / "truth.jsonl")
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    return detections, truth, manifest


def _read_dataset_args(
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    data_dir = Path(args.data)
    if data_dir.is_dir():
        return _load_dataset(data_dir)
    detections = read_jsonl(args.detections)
    truth = read_jsonl(args.truth)
    manifest = {}
    if args.manifest and Path(args.manifest).exists():
        manifest = read_json(args.manifest)
    return detections, truth, manifest


def command_generate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    paths = resolve_paths(_base_dir(args), config)
    dataset = generate_dataset(config)
    data_dir = Path(args.output) if args.output else paths["data"]
    fixture_dir = Path(args.fixtures) if args.fixtures else paths["fixtures"]
    write_dataset(dataset, data_dir)
    from .data_gen import make_error_fixtures

    fixtures = make_error_fixtures(dataset, fixture_dir)
    result = {
        "data_dir": str(data_dir.resolve()),
        "manifest": dataset["manifest"],
        "fixtures": fixtures,
    }
    print(pretty_json(result), end="")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    detections, truth, _ = _read_dataset_args(args)
    dataset = preprocess_dataset(detections, truth, config)
    result = {
        "status": "valid",
        "sequences": [
            {
                "seq_id": item.seq_id,
                "revision": item.revision,
                "frames": len(item.frames),
                "detections": len(item.detections_by_track),
                "truth_tracks": len(item.truth_by_track),
            }
            for item in dataset.sequences.values()
        ],
    }
    print(pretty_json(result), end="")
    return 0


def command_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    detections, truth, manifest = _read_dataset_args(args)
    paths = resolve_paths(_base_dir(args), config)
    runtime = Path(args.runtime) if args.runtime else paths["runtime"]
    output = Path(args.output) if args.output else paths["output"]
    engine = TrackingEngine(
        config,
        base_dir=_base_dir(args),
        runtime_dir=runtime,
        output_dir=output,
        crash_before_commit_frame=args.crash_before_commit_frame,
        crash_after_commit_frame=args.crash_after_commit_frame,
    )
    result = engine.run_dataset(detections, truth, dataset_manifest=manifest)
    printable = {
        "status": "complete",
        "runtime_dir": str(runtime.resolve()),
        "output_dir": str(output.resolve()),
        "sequences": [
            {
                "seq_id": item.seq_id,
                "revision": item.revision,
                "mode": item.report["mode"],
                "mota": item.metrics["mota"],
                "idf1": item.metrics["idf1"],
                "id_switches": item.metrics["id_switches"],
                "checkpoint_version": item.report["checkpoint_version"],
                "gap_completions": item.report["gap_completion_count"],
                "output_dir": str(item.output_dir),
            }
            for item in result["sequence_results"]
        ],
    }
    print(pretty_json(printable), end="")
    return 0


def command_revise(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    detections, truth, manifest = _read_dataset_args(args)
    dataset = {
        "detections": detections,
        "truth": truth,
        "manifest": manifest,
    }
    changes = json.loads(args.change)
    if not isinstance(changes, dict):
        raise TrackingError("E_INVALID_INPUT", "--change must be a JSON object")
    revised = mutate_detection(
        dataset,
        seq_id=args.seq_id,
        frame_index=args.frame,
        track_id_true=args.track_id,
        changes=changes,
        revision=args.revision,
    )
    output = Path(args.output)
    write_dataset(revised, output)
    print(
        pretty_json(
            {
                "status": "revised",
                "output": str(output.resolve()),
                "revision": revised["manifest"]["revision"],
                "accident": "revision patch",
                "changes": changes,
            }
        ),
        end="",
    )
    return 0


def command_serve(args: argparse.Namespace) -> int:
    from .webapp import run_server

    return run_server(
        base_dir=_base_dir(args),
        config_path=args.config,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


def _run_cli_subprocess(
    args: list[str],
    *,
    base_dir: Path,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    return subprocess.run(
        [sys.executable, "-m", "tracker_app", *args],
        cwd=str(base_dir),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _compare_output_dirs(
    first: Path,
    second: Path,
) -> dict[str, Any]:
    file_results: dict[str, bool] = {}
    for filename in COMPARABLE_OUTPUTS:
        first_path = first / filename
        second_path = second / filename
        file_results[filename] = (
            first_path.exists()
            and second_path.exists()
            and first_path.read_bytes() == second_path.read_bytes()
        )
    return {
        "equal": all(file_results.values()),
        "files": file_results,
        "first_sha256": {
            name: sha256_bytes((first / name).read_bytes())
            for name in COMPARABLE_OUTPUTS
            if (first / name).exists()
        },
    }


def _capture_error(callable_: Any) -> dict[str, Any]:
    try:
        callable_()
    except TrackingError as exc:
        return exc.to_dict()
    except Exception as exc:
        return {
            "code": "UNEXPECTED",
            "message": str(exc),
            "type": type(exc).__name__,
        }
    return {"code": None, "message": "no error raised"}


def _synthetic_gap_error(config: dict[str, Any]):
    from .gaps import complete_gaps
    from .models import Track

    bad_config = copy.deepcopy(config)
    bad_config["gap"]["manual_completion_plans"] = [
        {"seq_id": "synthetic", "track_id": 1, "gap_start": 2, "gap_end": 3},
        {"seq_id": "synthetic", "track_id": 1, "gap_start": 3, "gap_end": 4},
    ]
    observations = [
        {
            "frame_index": 0,
            "pred_track_id": 1,
            "track_id_true": 0,
            "bbox": [10.0, 10.0, 30.0, 50.0],
            "score": 0.9,
            "class_id": 0,
            "outcome": "matched",
        },
        {
            "frame_index": 10,
            "pred_track_id": 1,
            "track_id_true": 0,
            "bbox": [50.0, 10.0, 70.0, 50.0],
            "score": 0.9,
            "class_id": 0,
            "outcome": "matched",
        },
    ]
    track = Track(
        track_id=1,
        state="tracked",
        birth_frame=0,
        last_frame=10,
        last_bbox=(50.0, 10.0, 70.0, 50.0),
        appearance=(0.1, 0.2, 0.3),
        velocity=(4.0, 0.0, 0.0, 0.0),
    )
    complete_gaps(
        seq_id="synthetic",
        revision=1,
        assignments=observations,
        final_tracks={1: track},
        config=bad_config,
        frame_range=(0, 10),
    )


def command_demo(args: argparse.Namespace) -> int:
    base_dir = _base_dir(args)
    config = load_config(args.config)
    config_path = str(Path(args.config).resolve())
    demo_root = Path(args.output).resolve() if args.output else base_dir / "artifacts" / "demo"
    data_dir = demo_root / "data"
    fixture_dir = demo_root / "fixtures"
    full_runtime = demo_root / "runtime_full"
    full_output = demo_root / "output_full"
    crash_before_runtime = demo_root / "runtime_crash_before"
    crash_before_output = demo_root / "output_crash_before"
    crash_after_runtime = demo_root / "runtime_crash_after"
    crash_after_output = demo_root / "output_crash_after"
    revision_runtime = demo_root / "runtime_revision"
    revision_output = demo_root / "output"
    fresh_revision_runtime = demo_root / "runtime_revision_fresh"
    fresh_revision_output = demo_root / "output_revision_fresh"
    resource_runtime = demo_root / "runtime_resource_error"
    resource_output = demo_root / "output_resource_error"

    import shutil

    for path in (
        full_runtime,
        full_output,
        crash_before_runtime,
        crash_before_output,
        crash_after_runtime,
        crash_after_output,
        revision_runtime,
        revision_output,
        fresh_revision_runtime,
        fresh_revision_output,
        resource_runtime,
        resource_output,
    ):
        if path.exists():
            shutil.rmtree(path)

    dataset = generate_dataset(config)
    write_dataset(dataset, data_dir)
    from .data_gen import make_error_fixtures

    fixtures = make_error_fixtures(dataset, fixture_dir)

    full_engine = TrackingEngine(
        config,
        base_dir=base_dir,
        runtime_dir=full_runtime,
        output_dir=full_output,
    )
    full_result = full_engine.run_dataset(
        dataset["detections"], dataset["truth"], dataset_manifest=dataset["manifest"]
    )

    crash_frame = min(12, max(1, config["generator"]["frames"] // 3))
    before_process = _run_cli_subprocess(
        [
            "run",
            "--config",
            config_path,
            "--data",
            str(data_dir),
            "--runtime",
            str(crash_before_runtime),
            "--output",
            str(crash_before_output),
            "--crash-before-commit-frame",
            str(crash_frame),
        ],
        base_dir=base_dir,
    )
    before_resume = _run_cli_subprocess(
        [
            "run",
            "--config",
            config_path,
            "--data",
            str(data_dir),
            "--runtime",
            str(crash_before_runtime),
            "--output",
            str(crash_before_output),
        ],
        base_dir=base_dir,
    )
    after_process = _run_cli_subprocess(
        [
            "run",
            "--config",
            config_path,
            "--data",
            str(data_dir),
            "--runtime",
            str(crash_after_runtime),
            "--output",
            str(crash_after_output),
            "--crash-after-commit-frame",
            str(crash_frame),
        ],
        base_dir=base_dir,
    )
    after_resume = _run_cli_subprocess(
        [
            "run",
            "--config",
            config_path,
            "--data",
            str(data_dir),
            "--runtime",
            str(crash_after_runtime),
            "--output",
            str(crash_after_output),
        ],
        base_dir=base_dir,
    )

    crash_comparisons: dict[str, Any] = {}
    for seq_id in sorted(full_result["preprocessed"].seq_ids):
        full_seq = full_output / seq_id / "r000001"
        crash_comparisons[seq_id] = {
            "crash_before_commit": _compare_output_dirs(
                full_seq, crash_before_output / seq_id / "r000001"
            ),
            "crash_after_commit": _compare_output_dirs(
                full_seq, crash_after_output / seq_id / "r000001"
            ),
        }

    revised = mutate_detection(
        dataset,
        seq_id="seq-000",
        frame_index=20,
        track_id_true=0,
        changes={"occluded": False, "x1": 200.0, "x2": 240.0},
        revision=2,
    )
    revision_data_dir = demo_root / "data_revision"
    write_dataset(revised, revision_data_dir)
    revision_engine = TrackingEngine(
        config,
        base_dir=base_dir,
        runtime_dir=revision_runtime,
        output_dir=revision_output,
    )
    revision_engine.run_dataset(
        dataset["detections"], dataset["truth"], dataset_manifest=dataset["manifest"]
    )
    incremental_result = revision_engine.run_dataset(
        revised["detections"],
        revised["truth"],
        dataset_manifest=revised["manifest"],
    )
    fresh_engine = TrackingEngine(
        config,
        base_dir=base_dir,
        runtime_dir=fresh_revision_runtime,
        output_dir=fresh_revision_output,
    )
    fresh_revision_result = fresh_engine.run_dataset(
        revised["detections"],
        revised["truth"],
        dataset_manifest=revised["manifest"],
    )
    revision_comparison: dict[str, Any] = {}
    for seq_id in sorted(fresh_revision_result["preprocessed"].seq_ids):
        revision_comparison[seq_id] = _compare_output_dirs(
            revision_output / seq_id / "r000002",
            fresh_revision_output / seq_id / "r000002",
        )
    revision_store_checks: dict[str, Any] = {}
    from .storage import SequenceStore as DemoSequenceStore

    for seq_id in sorted(fresh_revision_result["preprocessed"].seq_ids):
        checkpoint_store = DemoSequenceStore(revision_runtime, seq_id)
        revision_checkpoints = checkpoint_store.validate_revision_chain(2)
        namespace_ok = all(
            checkpoint["tracker"]["mapping_namespace"] == f"{seq_id}:r2"
            for checkpoint in revision_checkpoints
        )
        seq0_reuse_ok = True
        if seq_id == "seq-000":
            seq0_reuse_ok = all(
                (
                    int(checkpoint["frame_index"]) < 20
                    and checkpoint.get("reused_from", {}).get("revision") == 1
                )
                or (
                    int(checkpoint["frame_index"]) >= 20
                    and checkpoint.get("reused_from") is None
                )
                for checkpoint in revision_checkpoints
            )
        revision_store_checks[seq_id] = {
            "mapping_namespace_revision_local": namespace_ok,
            "only_unaffected_prefix_reused": seq0_reuse_ok,
            "checkpoint_versions": [
                int(checkpoint["version"]) for checkpoint in revision_checkpoints
            ],
        }

    errors: list[dict[str, Any]] = []
    for name, fixture in fixtures.items():
        expected = fixture["expected_error"]
        try:
            preprocess_dataset(
                read_jsonl(fixture["path"]),
                dataset["truth"],
                config,
            )
            actual = {"code": None, "message": "no error raised"}
        except TrackingError as exc:
            actual = exc.to_dict()
        errors.append(
            {
                "case": name,
                "expected_code": expected,
                "actual": actual,
                "passed": actual.get("code") == expected,
            }
        )

    synthetic_gap = _capture_error(lambda: _synthetic_gap_error(config))
    errors.append(
        {
            "case": "synthetic_overlapping_gap",
            "expected_code": "E_GAP_INVALID",
            "actual": synthetic_gap,
            "passed": synthetic_gap.get("code") == "E_GAP_INVALID",
        }
    )

    bad_resource_config = copy.deepcopy(config)
    bad_resource_config["resources"]["policy"] = "error"
    bad_resource_config["resources"]["max_detections_per_frame"] = 1
    resource_error = _capture_error(
        lambda: TrackingEngine(
            bad_resource_config,
            base_dir=base_dir,
            runtime_dir=resource_runtime,
            output_dir=resource_output,
        ).run_dataset(
            dataset["detections"],
            dataset["truth"],
            dataset_manifest=dataset["manifest"],
        )
    )
    errors.append(
        {
            "case": "resource_limit",
            "expected_code": "E_RESOURCE_LIMIT",
            "actual": resource_error,
            "passed": resource_error.get("code") == "E_RESOURCE_LIMIT",
        }
    )

    tampered_metrics = copy.deepcopy(incremental_result["sequence_results"][0].metrics)
    tampered_metrics["mota"] = round(tampered_metrics["mota"] + 0.01, 10)
    from .metrics import verify_metric_recompute
    from .storage import SequenceStore, assemble_output_rows

    seq0 = incremental_result["preprocessed"].sequences["seq-000"]
    store = SequenceStore(revision_runtime, "seq-000")
    checkpoints = store.validate_revision_chain(2)
    assignments = assemble_output_rows(checkpoints, "assignments")
    consistency_error = _capture_error(
        lambda: verify_metric_recompute(
            tampered_metrics,
            assignments=assignments,
            truth_by_frame=seq0.truth_frames,
            completion_rows=incremental_result["sequence_results"][0].completion_rows,
            gap_reports=read_json(revision_output / "seq-000" / "r000002" / "gap_intervals.json"),
            segment_rows=incremental_result["sequence_results"][0].segments,
        )
    )
    errors.append(
        {
            "case": "metric_tamper",
            "expected_code": "E_INCONSISTENT",
            "actual": consistency_error,
            "passed": consistency_error.get("code") == "E_INCONSISTENT",
        }
    )

    from .models import Track

    terminated_track = Track(
        track_id=99,
        state="terminated",
        birth_frame=0,
        last_frame=4,
        last_bbox=(0.0, 0.0, 10.0, 10.0),
        appearance=(0.1, 0.2, 0.3),
        velocity=(0.0, 0.0, 0.0, 0.0),
        termination_frame=4,
    )
    machine = __import__("tracker_app.tracker", fromlist=["TrackerStateMachine"]).TrackerStateMachine(
        seq_id="synthetic",
        revision=1,
        config=config,
        tracks={99: terminated_track},
        next_track_id=100,
    )
    from .models import Detection

    illegal_detection = Detection(
        seq_id="synthetic",
        revision=1,
        frame_index=5,
        track_id_true=0,
        x1=0.0,
        y1=0.0,
        x2=10.0,
        y2=10.0,
        score=0.9,
        class_id=0,
        occluded=False,
        appearance=(0.1, 0.2, 0.3),
    )
    illegal_error = _capture_error(
        lambda: machine._apply_hit(terminated_track, illegal_detection, 5)
    )
    errors.append(
        {
            "case": "terminated_track_resurrection",
            "expected_code": "E_TRACK_ILLEGAL",
            "actual": illegal_error,
            "passed": illegal_error.get("code") == "E_TRACK_ILLEGAL",
        }
    )

    summary_sequences = []
    metrics_recompute_results: dict[str, dict[str, Any]] = {}
    from .metrics import compute_metrics

    for item in incremental_result["sequence_results"]:
        sequence_model = incremental_result["preprocessed"].sequences[item.seq_id]
        gap_reports = read_json(item.output_dir / "gap_intervals.json")
        independently_recomputed = compute_metrics(
            seq_id=item.seq_id,
            revision=item.revision,
            assignments=item.assignments,
            truth_by_frame=sequence_model.truth_frames,
            completion_rows=item.completion_rows,
            gap_reports=gap_reports,
            segment_rows=item.segments,
        )
        metric_keys = (
            "true_positives",
            "false_positives",
            "false_negatives",
            "id_switches",
            "mota",
            "idf1",
            "per_frame",
        )
        metrics_recompute_results[item.seq_id] = {
            "equal": all(
                independently_recomputed[key] == item.metrics[key]
                for key in metric_keys
            ),
            "keys": list(metric_keys),
        }
        summary_sequences.append(
            {
                "seq_id": item.seq_id,
                "revision": item.revision,
                "mode": item.report["mode"],
                "metrics": item.metrics,
                "checkpoint_report": item.report,
                "output_dir": str(item.output_dir),
            }
        )
    verification = {
        "schema": "mt-verification-v1",
        "environment": {
            "python": sys.version.split()[0],
            "numpy": __import__("numpy").__version__,
            "platform": sys.platform,
        },
        "config_path": config_path,
        "dataset_manifest": dataset["manifest"],
        "checks": {
            "crash_before_commit_terminated": before_process.returncode == 137,
            "crash_before_commit_resume_succeeded": before_resume.returncode == 0,
            "crash_after_commit_terminated": after_process.returncode == 137,
            "crash_after_commit_resume_succeeded": after_resume.returncode == 0,
            "crash_before_commit_recovery_equal": all(
                item["crash_before_commit"]["equal"]
                for item in crash_comparisons.values()
            ),
            "crash_after_commit_recovery_equal": all(
                item["crash_after_commit"]["equal"]
                for item in crash_comparisons.values()
            ),
            "revision_suffix_equal_to_fresh": all(
                item["equal"] for item in revision_comparison.values()
            ),
            "revision_mapping_namespace_local": all(
                item["mapping_namespace_revision_local"]
                for item in revision_store_checks.values()
            ),
            "revision_only_reuses_unaffected_prefix": all(
                item["only_unaffected_prefix_reused"]
                for item in revision_store_checks.values()
            ),
            "metrics_recompute_consistent": all(
                item["equal"] for item in metrics_recompute_results.values()
            ),
            "all_injected_errors_matched": all(item["passed"] for item in errors),
        },
        "crash_comparisons": crash_comparisons,
        "revision_comparison": revision_comparison,
        "revision_store_checks": revision_store_checks,
        "recovery_reports": {
            "crash_before": {
                "exit_code": before_process.returncode,
                "resume_exit_code": before_resume.returncode,
                "stdout": before_process.stdout.strip(),
                "stderr": before_process.stderr.strip(),
                "resume_stdout": before_resume.stdout.strip(),
            },
            "crash_after": {
                "exit_code": after_process.returncode,
                "resume_exit_code": after_resume.returncode,
                "stdout": after_process.stdout.strip(),
                "stderr": after_process.stderr.strip(),
                "resume_stdout": after_resume.stdout.strip(),
            },
            "resumed_sequence_reports": [
                read_json(
                    crash_before_output / seq_id / "r000001" / "run_report.json"
                )["recovery"]
                for seq_id in sorted(full_result["preprocessed"].seq_ids)
            ],
        },
        "sequences": summary_sequences,
        "metrics_recompute": metrics_recompute_results,
        "errors": errors,
        "fixtures": fixtures,
        "source_data": {
            "detections": str(data_dir / "detections.jsonl"),
            "truth": str(data_dir / "truth.jsonl"),
            "revision_data": str(revision_data_dir),
        },
    }
    atomic_write_json(demo_root / "verification.json", verification, pretty=True)
    error_catalog = {
        "schema": "mt-error-catalog-v1",
        "errors": errors,
    }
    atomic_write_json(demo_root / "error_catalog.json", error_catalog, pretty=True)
    print(pretty_json(verification), end="")
    return 0 if all(verification["checks"].values()) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker_app",
        description="Deterministic checkpointed multi-object tracking editor",
    )
    parser.add_argument("--base-dir", default=".", help="working base directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic sample data")
    generate.add_argument("--config", default="config/default.json")
    generate.add_argument("--output", default=None)
    generate.add_argument("--fixtures", default=None)
    generate.set_defaults(func=command_generate)

    validate = subparsers.add_parser("validate", help="preprocess and validate an input file")
    validate.add_argument("--config", default="config/default.json")
    validate.add_argument("--data", default="artifacts/demo/data")
    validate.add_argument("--detections", default=None)
    validate.add_argument("--truth", default=None)
    validate.add_argument("--manifest", default=None)
    validate.set_defaults(func=command_validate)

    run = subparsers.add_parser("run", help="run or resume tracking")
    run.add_argument("--config", default="config/default.json")
    run.add_argument("--data", default="artifacts/demo/data")
    run.add_argument("--detections", default=None)
    run.add_argument("--truth", default=None)
    run.add_argument("--manifest", default=None)
    run.add_argument("--runtime", default=None)
    run.add_argument("--output", default=None)
    run.add_argument("--crash-before-commit-frame", type=int, default=None)
    run.add_argument("--crash-after-commit-frame", type=int, default=None)
    run.set_defaults(func=command_run)

    revise = subparsers.add_parser("revise", help="write a higher-revision dataset")
    revise.add_argument("--config", default="config/default.json")
    revise.add_argument("--data", default="artifacts/demo/data")
    revise.add_argument("--detections", default=None)
    revise.add_argument("--truth", default=None)
    revise.add_argument("--manifest", default=None)
    revise.add_argument("--output", required=True)
    revise.add_argument("--seq-id", required=True)
    revise.add_argument("--frame", type=int, required=True)
    revise.add_argument("--track-id", type=int, default=None)
    revise.add_argument("--revision", type=int, required=True)
    revise.add_argument("--change", required=True, help="JSON object with patched fields")
    revise.set_defaults(func=command_revise)

    demo = subparsers.add_parser("demo", help="run end-to-end acceptance demo")
    demo.add_argument("--config", default="config/default.json")
    demo.add_argument("--output", default=None)
    demo.set_defaults(func=command_demo)

    serve = subparsers.add_parser("serve", help="start the local browser application")
    serve.add_argument("--config", default="config/default.json")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(func=command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"validate", "run", "revise"}:
        if args.detections is None and args.data is not None:
            args.detections = str(Path(args.data) / "detections.jsonl")
            args.truth = str(Path(args.data) / "truth.jsonl")
            args.manifest = str(Path(args.data) / "manifest.json")
    try:
        return int(args.func(args))
    except TrackingError as exc:
        print(pretty_json({"status": "error", **exc.to_dict()}), file=sys.stderr, end="")
        return 2
    except Exception as exc:
        if os.environ.get("TRACKER_DEBUG"):
            raise
        print(
            pretty_json(
                {
                    "status": "error",
                    "code": "UNEXPECTED",
                    "message": str(exc),
                    "type": type(exc).__name__,
                }
            ),
            file=sys.stderr,
            end="",
        )
        return 1
