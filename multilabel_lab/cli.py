"""Command-line entry points for classification, metrics, injection, and verify."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import (
    DEFAULT_ANNOTATION_SEED,
    DEFAULT_LABELS,
    DEFAULT_PROJECTION_SEED,
    DEFAULT_SEED,
    create_projection,
    default_config,
)
from .errors import E_CONFIG, E_SNAPSHOT_VERSION, E_UNATTRIBUTED, LabError, error_report
from .hashing import canonical_json_bytes, sha256_json
from .injection import load_injections
from .pipeline import classify_experiment
from .samples import build_default_payloads
from .snapshot import SnapshotStore
from .verifier import recompute_report_metrics, verify_experiment


def _add_experiment_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        default="artifacts/snapshots/default",
        help="directory containing the atomic snapshot.json bundle",
    )
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--annotation-seed", type=int, default=DEFAULT_ANNOTATION_SEED)
    parser.add_argument("--projection-seed", type=int, default=DEFAULT_PROJECTION_SEED)
    parser.add_argument("--width", type=int, default=12)
    parser.add_argument("--height", type=int, default=12)
    parser.add_argument("--grid-rows", type=int, default=3)
    parser.add_argument("--grid-cols", type=int, default=3)
    parser.add_argument(
        "--labels",
        default=",".join(DEFAULT_LABELS),
        help="comma-separated controlled label set",
    )
    parser.add_argument(
        "--uncertain-strategy",
        choices=("ignore", "positive", "separate"),
        default="ignore",
    )
    parser.add_argument(
        "--zero-division",
        choices=("zero", "one", "skip", "error"),
        default="zero",
    )
    parser.add_argument("--max-samples", type=int, default=64)
    parser.add_argument("--max-feature-dim", type=int, default=256)
    parser.add_argument(
        "--max-memory-bytes",
        type=int,
        default=64 * 1024 * 1024,
    )
    parser.add_argument(
        "--sample-policy",
        choices=("error", "truncate"),
        default="error",
    )
    parser.add_argument(
        "--feature-policy",
        choices=("error", "truncate"),
        default="error",
    )
    parser.add_argument(
        "--memory-policy",
        choices=("error", "truncate_samples"),
        default="error",
    )


def _requested_config(args: argparse.Namespace) -> dict[str, Any]:
    labels = [item.strip() for item in args.labels.split(",") if item.strip()]
    return default_config(
        seed=args.seed,
        labels=labels,
        width=args.width,
        height=args.height,
        grid_rows=args.grid_rows,
        grid_cols=args.grid_cols,
        uncertain_strategy=args.uncertain_strategy,
        zero_division=args.zero_division,
        max_samples=args.max_samples,
        max_feature_dim=args.max_feature_dim,
        max_resident_matrix_bytes=args.max_memory_bytes,
        sample_limit_policy=args.sample_policy,
        feature_limit_policy=args.feature_policy,
        memory_limit_policy=args.memory_policy,
    )


def _load_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    config = _requested_config(args)
    store = SnapshotStore(args.state_dir)
    if not store.path.exists():
        projection = create_projection(config, seed=args.projection_seed)
        payloads = build_default_payloads(
            config,
            sample_count=args.samples,
            annotation_seed=args.annotation_seed,
            projection=projection,
        )
        result = store.commit(
            payloads,
            versions={"config": 1, "projection": 1, "annotations": 1},
        )
        return result["snapshot"]

    result = store.load()
    snapshot = result["snapshot"]
    categories = snapshot["categories"]
    if canonical_json_bytes(categories["config"]["payload"]) != canonical_json_bytes(config):
        raise LabError(
            E_SNAPSHOT_VERSION,
            "state directory contains a different configuration; use a new state directory",
            stage="snapshot",
            details={
                "category": "config",
                "version": categories["config"]["version"],
            },
        )
    annotations = categories["annotations"]["payload"]
    if (
        annotations.get("sample_count") != args.samples
        or annotations.get("seed") != args.annotation_seed
    ):
        raise LabError(
            E_SNAPSHOT_VERSION,
            "requested sample count or annotation seed differs from the stored snapshot",
            stage="snapshot",
            details={
                "category": "annotations",
                "version": categories["annotations"]["version"],
                "stored_sample_count": annotations.get("sample_count"),
                "stored_seed": annotations.get("seed"),
            },
        )
    if categories["projection"]["payload"].get("seed") != args.projection_seed:
        raise LabError(
            E_SNAPSHOT_VERSION,
            "requested projection seed differs from the stored snapshot",
            stage="snapshot",
            details={
                "category": "projection",
                "version": categories["projection"]["version"],
            },
        )
    return snapshot


def _write_json(path: str | Path, value: Any) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(value) + b"\n"
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)


def _emit(path: str | Path, value: Any) -> None:
    _write_json(path, value)
    print(f"output={Path(path)}")
    print(f"status={value.get('status')} result_code={value.get('result_code')}")
    if "verification" in value:
        verification = value["verification"]
        print(
            "run_hash="
            f"{verification['first_hash']} "
            f"matched={verification['matched']}"
        )
    elif "matches_stored" in value:
        print(f"matches_stored={value['matches_stored']}")


def _run_verified(args: argparse.Namespace, injections: Any) -> dict[str, Any]:
    snapshot = _load_snapshot(args)
    return verify_experiment(snapshot, injections)


def _command_classify(args: argparse.Namespace) -> int:
    result = _run_verified(args, None)
    _emit(args.output, result)
    return 0 if result["result_code"] in ("OK", "OK_DEGRADED") else 2


def _command_inject(args: argparse.Namespace) -> int:
    injections = load_injections(args.injections)
    result = _run_verified(args, injections)
    _emit(args.output, result)
    return 0 if result["result_code"] in ("OK", "OK_DEGRADED") else 2


def _command_verify(args: argparse.Namespace) -> int:
    injections = load_injections(args.injections) if args.injections else None
    result = _run_verified(args, injections)
    _emit(args.output, result)
    return 0 if result["result_code"] in ("OK", "OK_DEGRADED") else 2


def _command_metrics(args: argparse.Namespace) -> int:
    try:
        raw = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LabError(
            E_CONFIG,
            "report cannot be read or parsed",
            stage="metrics",
            details={"error": type(exc).__name__, "path": args.report},
        ) from exc
    report = raw.get("report", raw)
    result = recompute_report_metrics(report)
    _emit(args.output, result)
    return 0 if result["matches_stored"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m multilabel_lab.cli",
        description="Deterministic multi-label classification and fault-injection lab",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify_parser = subparsers.add_parser(
        "classify", help="run and verify a normal local experiment"
    )
    _add_experiment_options(classify_parser)
    classify_parser.add_argument(
        "--output", default="artifacts/reports/normal.json"
    )
    classify_parser.set_defaults(handler=_command_classify)

    inject_parser = subparsers.add_parser(
        "inject", help="run and verify an experiment with a JSON injection plan"
    )
    _add_experiment_options(inject_parser)
    inject_parser.add_argument("--injections", required=True)
    inject_parser.add_argument(
        "--output", default="artifacts/reports/injection.json"
    )
    inject_parser.set_defaults(handler=_command_inject)

    verify_parser = subparsers.add_parser(
        "verify", help="rerun twice and verify complete-output consistency"
    )
    _add_experiment_options(verify_parser)
    verify_parser.add_argument("--injections")
    verify_parser.add_argument(
        "--output", default="artifacts/reports/verification.json"
    )
    verify_parser.set_defaults(handler=_command_verify)

    metrics_parser = subparsers.add_parser(
        "metrics", help="independently recompute metrics from report detail cells"
    )
    metrics_parser.add_argument("--report", required=True)
    metrics_parser.add_argument(
        "--output", default="artifacts/reports/metrics_recompute.json"
    )
    metrics_parser.set_defaults(handler=_command_metrics)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except LabError as error:
        result = error_report(error)
        if getattr(args, "output", None):
            _emit(args.output, result)
        else:
            print(canonical_json_bytes(result).decode("utf-8"))
        return 2
    except Exception as exc:
        wrapped = LabError(
            E_UNATTRIBUTED,
            "unattributed CLI exception",
            stage="cli",
            details={"error_type": type(exc).__name__, "message": str(exc)},
        )
        result = error_report(wrapped)
        if getattr(args, "output", None):
            _emit(args.output, result)
        else:
            print(canonical_json_bytes(result).decode("utf-8"))
        return 2


if __name__ == "__main__":
    sys.exit(main())
