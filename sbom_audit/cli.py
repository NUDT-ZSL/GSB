"""Command line interface: import, view, closure, checkpoint and verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointManager
from .codes import EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, RULESET_VERSIONS
from .engine import AuditEngine, Batch, BatchScheduler, Budget, stable_batch_summary
from .parser import parse_sync_map
from .storage import atomic_write_json, read_json


DEFAULT_LEDGER = Path(".sbom-audit/ledger.sqlite3")
DEFAULT_CHECKPOINT = Path(".sbom-audit/checkpoint.json")
DEFAULT_REPORT = Path(".sbom-audit/report.json")


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _load_sync(path: str | None, sample_dir: Path | None) -> dict[str, dict[str, Any]]:
    candidate = Path(path) if path else (sample_dir / "sync_markers.json" if sample_dir else None)
    if candidate and candidate.exists():
        return parse_sync_map(candidate)
    return {}


def _sample_path(sample_dir: Path | None, name: str) -> Path | None:
    if not sample_dir:
        return None
    path = sample_dir / name
    return path if path.exists() else None


def _load_batch_file(path: str | Path) -> list[Batch]:
    data = read_json(path)
    if isinstance(data, dict) and isinstance(data.get("batches"), list):
        return [
            Batch(
                batch_id=item["batch_id"],
                records=item.get("records", []),
                edges=item.get("edges", []),
                work_items=item.get("work_items", []),
                metadata=item,
            )
            for item in data["batches"]
        ]
    if not isinstance(data, dict) or "batch_id" not in data:
        raise ValueError("batch file must contain batch_id or batches[]")
    return [
        Batch(
            batch_id=data["batch_id"],
            records=data.get("records", []),
            edges=data.get("edges", []),
            work_items=data.get("work_items", []),
            metadata=data,
        )
    ]


def _build_import_jobs(args: argparse.Namespace) -> tuple[list[Batch], dict[str, Any]]:
    sample_dir = Path(args.sample_dir) if args.sample_dir else None
    v1_paths = [Path(p) for p in (args.v1 or [])]
    v2_paths = [Path(p) for p in (args.v2 or [])]
    dep_paths = [Path(p) for p in (args.dependencies or [])]
    batch_files = [Path(p) for p in (args.batch or [])]

    if not v1_paths and sample_dir:
        candidate = _sample_path(sample_dir, "v1_snapshot.json")
        if candidate:
            v1_paths.append(candidate)
    if not v2_paths and sample_dir:
        candidate = _sample_path(sample_dir, "v2")
        if candidate:
            v2_paths.append(candidate)
    if not dep_paths and sample_dir:
        candidate = _sample_path(sample_dir, "dependencies.json")
        if candidate:
            dep_paths.append(candidate)

    jobs: list[Batch] = []
    for index, path in enumerate(dep_paths, start=1):
        jobs.append(Batch(batch_id=f"dependencies-{index}-{path.stem}", source_kind="dependencies", source_path=path))
    for path in v1_paths:
        jobs.append(Batch(batch_id=f"v1-{path.stem}", source_kind="v1", source_path=path))
    for path in v2_paths:
        jobs.append(Batch(batch_id=f"v2-{path.name}", source_kind="v2", source_path=path, db_path=args.v2_db))
    for path in batch_files:
        jobs.extend(_load_batch_file(path))
    if not jobs:
        raise ValueError("no import inputs found; pass --sample-dir or --v1/--v2/--dependencies/--batch")
    return jobs, {"sample_dir": str(sample_dir) if sample_dir else None}


def cmd_import(args: argparse.Namespace) -> int:
    try:
        jobs, _ = _build_import_jobs(args)
        sync_markers = _load_sync(args.sync_map, Path(args.sample_dir) if args.sample_dir else None)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"argument/input error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    ledger_path = Path(args.ledger)
    checkpoint_path = Path(args.checkpoint)
    report_path = Path(args.report)
    if args.fresh:
        for path in [ledger_path, checkpoint_path, report_path]:
            path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(ledger_path) + suffix).unlink(missing_ok=True)

    budget = Budget(
        concurrency=max(1, args.concurrency),
        batch_timeout_ms=max(1, args.timeout_ms),
        queue_limit=max(0, args.queue_limit),
        max_digest_bytes_per_component=max(1, args.max_digest_bytes),
    )
    scheduler = BatchScheduler(ledger_path, budget, sync_markers)
    batch_reports = scheduler.run_all(jobs)

    engine = AuditEngine(ledger_path, sync_markers)
    stable_reports = sorted((stable_batch_summary(r) for r in batch_reports), key=lambda r: r.get("batch_id") or "")
    report = engine.full_report(batch_reports=stable_reports)
    report["scheduler"] = {
        "concurrency": budget.concurrency,
        "peak_active": scheduler.peak_active,
        "queue_limit": budget.queue_limit,
        "batch_timeout_ms": budget.batch_timeout_ms,
        "max_digest_bytes_per_component": budget.max_digest_bytes_per_component,
        "submitted_batch_count": len(jobs),
    }
    report["policy_versions"] = RULESET_VERSIONS
    # Recompute digest after adding scheduler metadata.
    from .merge import canonical_report_digest
    report["view_digest"] = canonical_report_digest(report)

    checkpoint = CheckpointManager(ledger_path, checkpoint_path)
    checkpoint.save(report)
    atomic_write_json(report_path, report)

    output = {
        "report_path": str(report_path),
        "checkpoint_path": str(checkpoint_path),
        "ledger_path": str(ledger_path),
        "status": report["status"],
        "summary": report["summary"],
        "global_codes": report["global_codes"],
        "closure": report["closure"],
        "peak_active": scheduler.peak_active,
    }
    _print_json(output)
    has_failure = report["status"] == "partial_failure" or any(r.get("status") in {"error", "rejected"} for r in batch_reports)
    if args.strict and (report["global_codes"] or report["closure"]):
        return EXIT_PARTIAL
    return EXIT_PARTIAL if has_failure else EXIT_OK


def _load_report_for_query(args: argparse.Namespace) -> dict[str, Any]:
    checkpoint_path = Path(getattr(args, "checkpoint", DEFAULT_CHECKPOINT))
    ledger_path = Path(getattr(args, "ledger", DEFAULT_LEDGER))
    if checkpoint_path.exists():
        try:
            checkpoint = read_json(checkpoint_path)
            report = checkpoint.get("report")
            if isinstance(report, dict):
                return report
        except Exception:
            pass
    engine = AuditEngine(ledger_path)
    return engine.full_report()


def cmd_view(args: argparse.Namespace) -> int:
    report = _load_report_for_query(args)
    views = report.get("integrity_view", [])
    if args.key:
        views = [v for v in views if v.get("canonical_key") == args.key or v.get("component_id") == args.key]
    if args.status:
        views = [v for v in views if v.get("conclusion") == args.status]
    if args.generation is not None:
        views = [v for v in views if v.get("authoritative_generation") == args.generation]
    if args.format == "table":
        print("canonical_key\tversion\tgen\tconclusion\tcodes")
        for view in views:
            print(f"{view['canonical_key']}\t{view.get('version')}\t{view.get('authoritative_generation')}\t{view.get('conclusion')}\t{','.join(view.get('codes', []))}")
    else:
        _print_json({"integrity_view": views, "count": len(views), "view_digest": report.get("view_digest")})
    return EXIT_OK if views or args.key is None else EXIT_PARTIAL


def cmd_closure(args: argparse.Namespace) -> int:
    report = _load_report_for_query(args)
    payload = {
        "schema": "sbom-audit.closure-report.v1",
        "view_digest": report.get("view_digest"),
        "seeds": report.get("seeds", []),
        "closure": report.get("closure", []),
        "invalid_closure": report.get("invalid_closure", []),
        "reverse_dependents": report.get("reverse_dependents", []),
        "closure_details": report.get("closure_details", {}),
        "version_violations": report.get("dependency", {}).get("version_violations", []),
        "cycles": report.get("dependency", {}).get("cycles", []),
        "global_codes": report.get("global_codes", []),
    }
    if args.out:
        atomic_write_json(args.out, payload)
        _print_json({"written": args.out, "closure_size": len(payload["closure"])})
    else:
        _print_json(payload)
    return EXIT_PARTIAL if payload["cycles"] else EXIT_OK


def cmd_checkpoint(args: argparse.Namespace) -> int:
    manager = CheckpointManager(args.ledger, args.checkpoint)
    result = manager.verify()
    if args.recover and not result.get("valid"):
        engine = AuditEngine(args.ledger)
        if engine.records or engine.edges:
            report = engine.full_report()
            manager.save(report)
            recovered = manager.verify()
            result["recovery"] = {"rebuilt_from_ledger": True, "verification": recovered}
            result["valid"] = recovered.get("valid", False)
        else:
            result["recovery"] = {
                "rebuilt": False,
                "message": "ledger has no raw records; re-run import-snapshots from original snapshots",
            }
    _print_json(result)
    return EXIT_OK if result.get("valid") else EXIT_PARTIAL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sbom-audit", description="Deterministic software supply-chain audit toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser(
        "import-snapshots",
        aliases=["import", "import_snapshots"],
        help="import v1/v2 snapshots, dependencies and incremental batches",
    )
    imp.add_argument("--sample-dir", default="samples/generated", help="directory produced by generate-sample")
    imp.add_argument("--v1", action="append", help="v1 snapshot JSON; repeatable")
    imp.add_argument("--v2", action="append", help="v2 snapshot directory or shard file; repeatable")
    imp.add_argument("--v2-db", help="explicit v2 SQLite metadata path for file shards")
    imp.add_argument("--dependencies", action="append", help="dependency declaration JSON; repeatable")
    imp.add_argument("--batch", action="append", help="incremental batch JSON; repeatable")
    imp.add_argument("--sync-map", help="sync marker declaration JSON")
    imp.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    imp.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    imp.add_argument("--report", default=str(DEFAULT_REPORT))
    imp.add_argument("--fresh", action="store_true", help="delete local ledger/checkpoint/report before import")
    imp.add_argument("--concurrency", type=int, default=2)
    imp.add_argument("--timeout-ms", type=int, default=10_000)
    imp.add_argument("--queue-limit", type=int, default=16)
    imp.add_argument("--max-digest-bytes", type=int, default=64 * 1024 * 1024)
    imp.add_argument("--strict", action="store_true", help="return partial exit when security findings exist")
    imp.set_defaults(func=cmd_import)

    view = sub.add_parser("query-view", aliases=["view", "query_view"], help="query the unified integrity view")
    view.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    view.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    view.add_argument("--key")
    view.add_argument("--status", choices=["valid", "affected", "invalid"])
    view.add_argument("--generation", type=int)
    view.add_argument("--format", choices=["json", "table"], default="json")
    view.set_defaults(func=cmd_view)

    closure = sub.add_parser("export-closure", aliases=["closure", "export_closure"], help="export invalidation closure report")
    closure.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    closure.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    closure.add_argument("--out")
    closure.set_defaults(func=cmd_closure)

    cp = sub.add_parser(
        "verify-checkpoint",
        aliases=["checkpoint", "checkpoint-verify", "verify_checkpoint"],
        help="verify SQLite/JSON checkpoint and recovery boundary",
    )
    cp.add_argument("action", nargs="?", choices=["verify"], default="verify")
    cp.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cp.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    cp.add_argument("--recover", action="store_true", help="fully rebuild the checkpoint from raw ledger records when verification fails")
    cp.set_defaults(func=cmd_checkpoint)

    gen = sub.add_parser("generate-sample", help="generate the fixed-seed local sample")
    gen.add_argument("--root", default="samples/generated")

    def _gen(args: argparse.Namespace) -> int:
        from .samplegen import main as gen_main

        sys.argv = ["generate_sample", "--root", args.root]
        gen_main()
        return EXIT_OK

    gen.set_defaults(func=_gen)

    verify = sub.add_parser("verify-sample", aliases=["verify_sample"], help="run deterministic truth-file acceptance checks")
    verify.add_argument("--sample-dir", default="samples/generated")
    verify.add_argument("--work-dir", default=".verify-work")
    verify.add_argument("--json", action="store_true")

    def _verify(args: argparse.Namespace) -> int:
        from .verify import verify_sample

        result = verify_sample(args.sample_dir, args.work_dir)
        if args.json:
            _print_json(result)
        else:
            print(f"PASS: {result['checks_passed']} deterministic checks")
        return EXIT_OK

    verify.set_defaults(func=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"file not found: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"invalid argument: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
