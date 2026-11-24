"""Command line interface for deterministic supply-chain audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointManager
from .codes import E_RECOMPUTE_DIVERGE
from .generator import generate_seed_workspace
from .importer import BatchImporter, ImportBudget
from .io_utils import atomic_write_json
from .parsing import load_clock, load_dependencies, load_revocation_batch
from .report import build_closure_report, build_query_result
from .service import build_seed_engine


def _print_json(value: dict[str, Any]) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _resolve_sample_defaults(args: argparse.Namespace) -> None:
    sample_dir = getattr(args, "sample_dir", None)
    if not sample_dir:
        return
    root = Path(sample_dir)
    args.clock = args.clock or str(root / "observation_clock.json")
    args.dependencies = args.dependencies or str(root / "dependencies.json")
    if not args.snapshot:
        args.snapshot = [str(root / "v1_snapshot.json"), str(root / "v2_manifest.json")]
        if getattr(args, "apply_seed_updates", False):
            args.snapshot.append(str(root / "batches" / "update_codec_digest.json"))
    if not args.revocation and getattr(args, "apply_seed_updates", False):
        args.revocation = [str(root / "batches" / "revoke_log.json")]
    if getattr(args, "include_corrupt", False):
        args.snapshot.append(str(root / "batches" / "corrupt_snapshot.json"))


def _command_generate(args: argparse.Namespace) -> int:
    paths = generate_seed_workspace(args.output)
    _print_json({"status": "generated", "paths": paths})
    return 0


def _command_import(args: argparse.Namespace) -> int:
    _resolve_sample_defaults(args)
    if not args.clock:
        raise SystemExit("import requires --clock or --sample-dir")
    clock = load_clock(args.clock)
    dependencies = load_dependencies(args.dependencies) if args.dependencies else {"edges": [], "rejections": []}

    from .engine import AuditEngine

    engine = AuditEngine(clock=clock, dependencies=dependencies)
    budget = ImportBudget(
        concurrency=args.concurrency,
        timeout_seconds=args.timeout_seconds,
        queue_limit=args.queue_limit,
        max_hash_operations=args.max_hash_operations,
    )
    importer = BatchImporter(clock=clock, budget=budget, engine=engine)
    base_dir = args.sample_dir or str(Path(args.snapshot[0]).parent) if args.snapshot else None
    batches = [
        {"batch_id": Path(path).stem, "path": path, "base_dir": base_dir}
        for path in (args.snapshot or [])
    ]
    import_report = importer.import_batches(batches)

    revocation_outcomes = []
    for revocation_path in args.revocation or []:
        batch = load_revocation_batch(revocation_path)
        outcome = engine.apply_revocation_batch(batch)
        revocation_outcomes.append(outcome)

    checkpoint_manager = CheckpointManager(args.workspace)
    checkpoint = checkpoint_manager.save_checkpoint(engine.view, engine.applied_batches)
    report = {
        **import_report,
        "revocation_outcomes": revocation_outcomes,
        "checkpoint": checkpoint,
        "view_stats": engine.view["stats"],
    }
    divergences = [
        outcome.get("apply", {}).get("divergence")
        for outcome in report["outcomes"]
        if outcome.get("apply", {}).get("status") == E_RECOMPUTE_DIVERGE
    ] + [
        outcome.get("divergence")
        for outcome in revocation_outcomes
        if outcome.get("status") == E_RECOMPUTE_DIVERGE
    ]
    if divergences:
        report["divergences"] = divergences

    report_path = Path(args.report) if args.report else Path(args.workspace) / "import-report.json"
    atomic_write_json(report_path, report)
    report["report_path"] = str(report_path)
    _print_json(report)
    return 3 if divergences else 0


def _load_valid_view(workspace: str, rebuild: bool = False, sample_dir: str | None = None):
    manager = CheckpointManager(workspace)
    verification = manager.verify_latest()
    if verification.get("status") == "valid":
        return manager, verification, verification["view"]
    if rebuild and sample_dir:
        engine = build_seed_engine(sample_dir, include_update=True, include_revocation=True)
        checkpoint = manager.save_checkpoint(engine.view, engine.applied_batches)
        verification = manager.verify_latest()
        verification["rebuilt_checkpoint"] = checkpoint
        return manager, verification, engine.view
    return manager, verification, None


def _command_query(args: argparse.Namespace) -> int:
    _, verification, view = _load_valid_view(args.workspace, args.rebuild, args.sample_dir)
    if view is None:
        result = {key: value for key, value in verification.items() if key != "view"}
        if args.out:
            atomic_write_json(args.out, result)
        _print_json(result)
        return 2
    result = build_query_result(view, args.key, args.status)
    if args.out:
        atomic_write_json(args.out, result)
    _print_json(result)
    return 0


def _command_export(args: argparse.Namespace) -> int:
    _, verification, view = _load_valid_view(args.workspace, args.rebuild, args.sample_dir)
    if view is None:
        result = {key: value for key, value in verification.items() if key != "view"}
        if args.out:
            atomic_write_json(args.out, result)
        _print_json(result)
        return 2
    report = build_closure_report(view, args.source_key)
    output_path = Path(args.out) if args.out else Path(args.workspace) / "closure-report.json"
    atomic_write_json(output_path, report)
    report["report_path"] = str(output_path)
    _print_json(report)
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    manager = CheckpointManager(args.workspace)
    if args.force_rebuild:
        if not args.sample_dir:
            raise SystemExit("--force-rebuild requires --sample-dir")
        engine = build_seed_engine(
            args.sample_dir,
            include_update=args.apply_seed_updates,
            include_revocation=args.apply_seed_updates,
        )
        saved = manager.save_checkpoint(engine.view, engine.applied_batches)
        result = {"status": "force_rebuilt", "checkpoint": saved}
    else:
        verification = manager.verify_latest()
        if verification.get("rebuild_required") and args.rebuild and args.sample_dir:
            engine = build_seed_engine(
                args.sample_dir,
                include_update=args.apply_seed_updates,
                include_revocation=args.apply_seed_updates,
            )
            saved = manager.save_checkpoint(engine.view, engine.applied_batches)
            result = {
                "status": "rebuilt",
                "previous": {k: v for k, v in verification.items() if k != "view"},
                "checkpoint": saved,
            }
        else:
            result = {key: value for key, value in verification.items() if key != "view"}
    if args.out:
        atomic_write_json(args.out, result)
    _print_json(result)
    if result.get("code") in {"E_CHECKPOINT_CORRUPT", "E_CHECKPOINT_MISSING", "E_CHECKPOINT_VERSION"}:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audit", description="Software supply-chain integrity auditor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-sample", help="generate fixed-seed local samples")
    generate.add_argument("output", nargs="?", default="examples/seed_workspace")
    generate.set_defaults(func=_command_generate)

    imp = subparsers.add_parser(
        "import-snapshots",
        aliases=["import", "import-snapshot", "import_snapshots"],
        help="import v1/v2 snapshots",
    )
    imp.add_argument("--workspace", default=".audit-workspace")
    imp.add_argument("--sample-dir")
    imp.add_argument("--clock")
    imp.add_argument("--dependencies")
    imp.add_argument("--snapshot", action="append")
    imp.add_argument("--revocation", action="append")
    imp.add_argument("--concurrency", type=int, default=2)
    imp.add_argument("--timeout-seconds", type=float)
    imp.add_argument("--queue-limit", type=int)
    imp.add_argument("--max-hash-operations", type=int)
    imp.add_argument("--report")
    imp.add_argument("--apply-seed-updates", action="store_true")
    imp.add_argument("--include-corrupt", action="store_true")
    imp.set_defaults(func=_command_import)

    query = subparsers.add_parser(
        "query-view",
        aliases=["query", "query-integrity", "query-integrity-view"],
        help="query integrity view",
    )
    query.add_argument("--workspace", default=".audit-workspace")
    query.add_argument("--key")
    query.add_argument("--status")
    query.add_argument("--out")
    query.add_argument("--rebuild", action="store_true")
    query.add_argument("--sample-dir")
    query.set_defaults(func=_command_query)

    export = subparsers.add_parser(
        "export-closure",
        aliases=["export", "export-closure-report", "closure-report"],
        help="export invalidation closure report",
    )
    export.add_argument("--workspace", default=".audit-workspace")
    export.add_argument("--source-key")
    export.add_argument("--out")
    export.add_argument("--rebuild", action="store_true")
    export.add_argument("--sample-dir")
    export.set_defaults(func=_command_export)

    verify = subparsers.add_parser(
        "verify-checkpoint",
        aliases=["checkpoint", "verify", "validate-checkpoint"],
        help="verify checkpoint recovery boundary",
    )
    verify.add_argument("--workspace", default=".audit-workspace")
    verify.add_argument("--sample-dir")
    verify.add_argument("--rebuild", action="store_true")
    verify.add_argument("--force-rebuild", action="store_true")
    verify.add_argument("--apply-seed-updates", action="store_true")
    verify.add_argument("--out")
    verify.set_defaults(func=_command_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
