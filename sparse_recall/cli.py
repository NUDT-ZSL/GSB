"""Command line examples for generation, planning, execution, and resume."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_json_text, read_json, sha256_file, write_json_atomic
from .errors import RecallError
from .ingest import ingest_dataset
from .pipeline import PipelineConfig, run_pipeline
from .sample_data import generate_dataset
from .scheduler import SchedulerConfig, build_plan, make_scheduler_config
from .snapshots import VersionedSnapshotStore, checkpoint_file
from .weights import load_weight_table


def _print(value: Any) -> None:
    print(canonical_json_text(value))


def _data_paths(data_dir: str | Path) -> tuple[Path, Path, Path]:
    base = Path(data_dir)
    return (
        base / "documents.jsonl",
        base / "queries.jsonl",
        base / "term_weights.json",
    )


def _load_plan_inputs(
    data_dir: str | Path,
    *,
    slots: int,
    fma_budget: int,
    memory_budget: int,
    workers: int,
    chunk_size: int,
) -> tuple[Any, Any, SchedulerConfig, dict[str, Any]]:
    documents_path, queries_path, weights_path = _data_paths(data_dir)
    weights = load_weight_table(str(weights_path))
    ingested = ingest_dataset(documents_path, queries_path, weights)
    scheduler_config = make_scheduler_config(
        ingested,
        weights,
        slots=slots,
        fma_budget=fma_budget,
        memory_budget=memory_budget,
        workers=workers,
        chunk_size=chunk_size,
    )
    plan = build_plan(ingested, weights, scheduler_config)
    return ingested, weights, scheduler_config, plan


def _fresh_workdir(workdir: Path) -> None:
    candidates = [
        workdir / "plan.json",
        workdir / "run_report.json",
        workdir / "recovery_report.json",
        workdir / "checkpoints" / "checkpoint.json",
        workdir / "checkpoints" / "statistics.json",
        workdir / "outputs" / "vectors.jsonl",
        workdir / "outputs" / "results.jsonl",
        workdir / "outputs" / "validation.jsonl",
        workdir / "outputs" / "statistics.json",
        workdir / "outputs" / "inverted_index.json",
        workdir / "outputs" / "plan.json",
        workdir / "outputs" / "failures.jsonl",
        workdir / "outputs" / "consistency.json",
    ]
    for path in candidates:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _parse_failure(
    *,
    batch_key: str | None,
    stage: str | None,
    code: str,
    message: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    if not batch_key or not stage:
        return {}
    return {
        batch_key: {
            stage: {
                "code": code,
                "message": message,
            }
        }
    }


def command_generate(args: argparse.Namespace) -> int:
    manifest = generate_dataset(
        args.out,
        seed=args.seed,
        doc_count=args.doc_count,
        doc_batches=args.doc_batches,
        query_count=args.query_count,
        query_batches=args.query_batches,
        vocabulary_size=args.vocabulary_size,
        dimension=args.dimension,
        nnz_per_term=args.nnz_per_term,
    )
    _print(manifest)
    return 0


def command_plan(args: argparse.Namespace) -> int:
    _, _, _, plan = _load_plan_inputs(
        args.data_dir,
        slots=args.slots,
        fma_budget=args.fma_budget,
        memory_budget=args.memory_budget,
        workers=args.workers,
        chunk_size=args.chunk_size,
    )
    target = Path(args.workdir)
    write_json_atomic(target / "plan.json", plan)
    _print(
        {
            "plan": str(target / "plan.json"),
            "totals": plan["totals"],
            "decisions": [
                {
                    "batch_key": item["batch_key"],
                    "status": item["status"],
                    "code": item["code"],
                    "execution": item["execution"],
                    "reason_codes": item["reason_codes"],
                }
                for item in plan["decisions"]
            ],
        }
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    if args.fresh:
        _fresh_workdir(workdir)
    documents_path, queries_path, weights_path = _data_paths(args.data_dir)
    _, _, scheduler_config, _ = _load_plan_inputs(
        args.data_dir,
        slots=args.slots,
        fma_budget=args.fma_budget,
        memory_budget=args.memory_budget,
        workers=args.workers,
        chunk_size=args.chunk_size,
    )
    config = PipelineConfig(
        workers=args.workers,
        use_process_pool=not args.serial,
        chunk_size=args.chunk_size,
        compensated=not args.no_compensation,
        decimals=args.decimals,
        subnormal_floor=args.subnormal_floor,
        threshold=args.threshold,
        compare_mode=args.compare_mode,
        tolerance=args.tolerance,
        crash_after_commits=args.crash_after_commits,
        injected_failures=_parse_failure(
            batch_key=args.fail_batch_key,
            stage=args.fail_stage,
            code=args.fail_code,
            message=args.fail_message,
        ),
    )
    result = run_pipeline(
        document_path=documents_path,
        query_path=queries_path,
        weights_path=weights_path,
        workdir=workdir,
        scheduler_config=scheduler_config,
        config=config,
    )
    _print(
        {
            "status": result.report["status"],
            "run_report": str(workdir / "run_report.json"),
            "outputs": str(result.output_dir),
            "plan": result.report["plan"],
            "checkpoint": result.report["checkpoint"],
            "artifact_sha256": result.report["artifact_sha256"],
        }
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    report = read_json(workdir / "run_report.json")
    consistency = read_json(workdir / "outputs" / "consistency.json")
    mismatches: dict[str, dict[str, str]] = {}
    for name, expected in sorted(report.get("artifact_sha256", {}).items()):
        actual = sha256_file(workdir / "outputs" / name)
        if actual != expected:
            mismatches[name] = {"expected": expected, "actual": actual}
    checkpoint = VersionedSnapshotStore(
        checkpoint_file(workdir),
        snapshot_kind="pipeline-checkpoint",
    ).load()
    final_version = int(report.get("checkpoint", {}).get("final_version", -1))
    checkpoint_ok = (
        final_version == 0
        if checkpoint is None
        else int(checkpoint["version"]) == final_version
    )
    ok = (
        report.get("status") == "ok"
        and consistency.get("status") == "ok"
        and not mismatches
        and checkpoint_ok
    )
    _print(
        {
            "ok": ok,
            "run_report": str(workdir / "run_report.json"),
            "consistency": consistency,
            "artifact_mismatches": mismatches,
            "checkpoint_ok": checkpoint_ok,
        }
    )
    return 0 if ok else 2


def command_demo(args: argparse.Namespace) -> int:
    root = Path(args.root)
    data_dir = root / "data"
    run_dir = root / "run"
    manifest = generate_dataset(
        data_dir,
        seed=args.seed,
        doc_count=72,
        doc_batches=6,
        query_count=30,
        query_batches=3,
        vocabulary_size=48,
        dimension=64,
        nnz_per_term=3,
    )
    if args.fresh:
        _fresh_workdir(run_dir)
    documents_path, queries_path, weights_path = _data_paths(data_dir)
    _, _, scheduler_config, plan = _load_plan_inputs(
        data_dir,
        slots=2,
        fma_budget=args.fma_budget,
        memory_budget=args.memory_budget,
        workers=2,
        chunk_size=8,
    )
    config = PipelineConfig(
        workers=2,
        use_process_pool=not args.serial,
        chunk_size=8,
        threshold=0.0,
        compare_mode="tolerance",
        tolerance=1.0e-9,
    )
    result = run_pipeline(
        document_path=documents_path,
        query_path=queries_path,
        weights_path=weights_path,
        workdir=run_dir,
        scheduler_config=scheduler_config,
        config=config,
    )
    summary = {
        "schema": "sparse-recall.demo.v1",
        "dataset": manifest,
        "dataset_dir": str(data_dir),
        "run_dir": str(run_dir),
        "run_report": str(run_dir / "run_report.json"),
        "plan": plan["totals"],
        "artifacts": result.report["artifact_sha256"],
    }
    write_json_atomic(root / "demo_report.json", summary)
    _print(summary)
    return 0


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", default="data/demo")
    parser.add_argument("--workdir", default="artifacts/run")
    parser.add_argument("--slots", type=int, default=2)
    parser.add_argument("--fma-budget", type=int, default=22000)
    parser.add_argument("--memory-budget", type=int, default=20000)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument("--decimals", type=int, default=12)
    parser.add_argument("--subnormal-floor", type=float, default=1.0e-300)
    parser.add_argument("--threshold", type=float, default=0.0)
    parser.add_argument(
        "--compare-mode",
        choices=("strict", "tolerance"),
        default="tolerance",
    )
    parser.add_argument("--tolerance", type=float, default=1.0e-9)
    parser.add_argument("--no-compensation", action="store_true")
    parser.add_argument("--serial", action="store_true", help="disable spawn pool")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument(
        "--crash-after-commits",
        type=int,
        default=None,
        help="test-only hard exit after N committed batch boundaries",
    )
    parser.add_argument("--fail-batch-key", default=None)
    parser.add_argument(
        "--fail-stage",
        choices=("inverted", "projection"),
        default=None,
    )
    parser.add_argument("--fail-code", default="E_WORKER")
    parser.add_argument("--fail-message", default="injected worker failure")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparse-recall")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate local JSONL samples")
    generate.add_argument("--out", default="data/demo")
    generate.add_argument("--seed", type=int, default=20261124)
    generate.add_argument("--doc-count", type=int, default=72)
    generate.add_argument("--doc-batches", type=int, default=6)
    generate.add_argument("--query-count", type=int, default=30)
    generate.add_argument("--query-batches", type=int, default=3)
    generate.add_argument("--vocabulary-size", type=int, default=48)
    generate.add_argument("--dimension", type=int, default=64)
    generate.add_argument("--nnz-per-term", type=int, default=3)
    generate.set_defaults(func=command_generate)

    plan = subparsers.add_parser("plan", help="write a deterministic admission plan")
    plan.add_argument("--data-dir", default="data/demo")
    plan.add_argument("--workdir", default="artifacts/plan")
    plan.add_argument("--slots", type=int, default=2)
    plan.add_argument("--fma-budget", type=int, default=22000)
    plan.add_argument("--memory-budget", type=int, default=20000)
    plan.add_argument("--workers", type=int, default=2)
    plan.add_argument("--chunk-size", type=int, default=8)
    plan.set_defaults(func=command_plan)

    run = subparsers.add_parser("run", help="execute or resume the local pipeline")
    _add_run_arguments(run)
    run.set_defaults(func=command_run)

    verify = subparsers.add_parser("verify", help="verify the latest run report")
    verify.add_argument("--workdir", default="artifacts/run")
    verify.set_defaults(func=command_verify)

    demo = subparsers.add_parser("demo", help="generate and run the complete local demo")
    demo.add_argument("--root", default="artifacts/demo")
    demo.add_argument("--seed", type=int, default=20261124)
    demo.add_argument("--fma-budget", type=int, default=22000)
    demo.add_argument("--memory-budget", type=int, default=20000)
    demo.add_argument("--serial", action="store_true")
    demo.add_argument("--fresh", action="store_true")
    demo.set_defaults(func=command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RecallError as exc:
        _print({"status": "error", "error": exc.to_dict()})
        return 2


if __name__ == "__main__":
    sys.exit(main())
