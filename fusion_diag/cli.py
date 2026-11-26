from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_json,
    canonical_bytes,
    canonical_json,
    digest_json,
)
from .config import (
    ConfigSnapshot,
    load_config,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
)
from .engine import (
    SimulatedCrash,
    load_workspace,
    query_one,
    run_batch,
)
from .errors import DiagError
from .generate import generate_workspace
from .ingest import (
    load_documents,
    load_queries,
    load_vocabulary,
    prepare_corpus,
    write_prepared,
)
from .state import StageLock


def _emit(value: Any) -> None:
    sys.stdout.write(canonical_json(value) + "\n")


def _workspace_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--docs", required=True, help="document JSONL path")
    parser.add_argument("--queries", required=True, help="query JSONL path")
    parser.add_argument("--vocab", required=True, help="controlled vocabulary")
    parser.add_argument("--config", required=True, help="v1 JSON or v2 directory")


def command_generate(args: argparse.Namespace) -> int:
    manifest = generate_workspace(args.out, seed=args.seed)
    _emit(manifest)
    return 0


def command_validate(args: argparse.Namespace) -> int:
    vocab = load_vocabulary(args.vocab)
    documents = load_documents(args.docs, vocab)
    corpus = prepare_corpus(documents, vocab)
    queries = load_queries(args.queries, categories=corpus.categories)
    config = load_config(args.config)
    result: dict[str, Any] = {
        "status": "ok",
        "documents": len(documents),
        "queries": len(queries),
        "categories": list(corpus.categories),
        "average_doc_len": corpus.stats["average_doc_len"],
        "document_content_hash": corpus.content_hash,
        "glossary_hash": corpus.glossary_hash,
        "queries_content_hash": digest_json(
            [query.content_dict() for query in queries]
        ),
        "config": config.reference(),
    }
    if args.prepared_out:
        result["prepared_output_hash"] = write_prepared(
            corpus, args.prepared_out
        )
        result["prepared_output_path"] = args.prepared_out
    if args.stats_out:
        atomic_write_json(args.stats_out, corpus.stats)
        result["stats_output_path"] = args.stats_out
    _emit(result)
    return 0


def command_batch(args: argparse.Namespace) -> int:
    workspace = load_workspace(
        docs_path=args.docs,
        queries_path=args.queries,
        vocab_path=args.vocab,
        config_path=args.config,
    )
    report: dict[str, Any] | None = None
    lock = (
        StageLock(Path(args.workdir) / "stage.lock")
        if not args.no_lock
        else _NullLock()
    )
    with lock:
        report = run_batch(
            workspace,
            workdir=args.workdir,
            output_path=args.out,
            mode=args.mode,
            use_cache=not args.no_cache,
            resume=not args.no_resume,
            crash_after=args.crash_after,
            verify_full=args.verify_full,
        )
    if args.report:
        atomic_write_json(args.report, report)
    _emit(report)
    return 0


class _NullLock:
    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


def _load_batch_line(path: str, query_id: str) -> bytes | None:
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if value.get("query_id") == query_id:
                return canonical_bytes(value)
    return None


def command_query(args: argparse.Namespace) -> int:
    workspace = load_workspace(
        docs_path=args.docs,
        queries_path=args.queries,
        vocab_path=args.vocab,
        config_path=args.config,
    )
    if args.query_id not in workspace.query_by_id:
        raise DiagError(
            "E_FORMAT_CORRUPT",
            "query_id not found",
            field="query_id",
        )
    lock = (
        StageLock(Path(args.workdir) / "stage.lock")
        if not args.no_lock
        else _NullLock()
    )
    samples: list[float] = []
    result: dict[str, Any] | None = None
    last_timing: dict[str, Any] | None = None
    with lock:
        for _ in range(args.repeat):
            result, timing = query_one(
                workspace,
                args.query_id,
                workdir=args.workdir,
                use_cache=not args.no_cache,
            )
            samples.append(float(timing["observed_ms"]))
            last_timing = timing
    assert result is not None
    assert last_timing is not None
    result_sha = digest_json(result)
    comparison: dict[str, Any] | None = None
    if args.compare_batch:
        batch_bytes = _load_batch_line(args.compare_batch, args.query_id)
        if batch_bytes is None:
            comparison = {
                "status": "missing_query",
                "query_id": args.query_id,
            }
        else:
            comparison = {
                "status": (
                    "byte_identical"
                    if canonical_bytes(result) == batch_bytes
                    else "mismatch"
                ),
                "query_id": args.query_id,
                "batch_sha256": hashlib.sha256(batch_bytes).hexdigest(),
                "interactive_sha256": result_sha,
            }
    if args.raw:
        sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
        if comparison and comparison["status"] != "byte_identical":
            sys.stderr.write(canonical_json(comparison) + "\n")
            return 4
        return 0
    sorted_samples = sorted(samples)
    p50 = statistics.median(sorted_samples)
    p95_index = min(
        len(sorted_samples) - 1,
        max(0, int(round(0.95 * (len(sorted_samples) - 1)))),
    )
    envelope = {
        "schema": "fusion_diag.interactive_query.v1",
        "result": result,
        "result_canonical_sha256": result_sha,
        "latency": {
            "repeat": args.repeat,
            "samples_ms": [round(value, 6) for value in samples],
            "mean_ms": round(statistics.fmean(samples), 6),
            "p50_ms": round(p50, 6),
            "p95_ms": round(sorted_samples[p95_index], 6),
            "features": last_timing["features"],
            "estimate_model": last_timing["estimate_model"],
        },
    }
    if comparison is not None:
        envelope["batch_comparison"] = comparison
    _emit(envelope)
    if comparison and comparison["status"] != "byte_identical":
        return 4
    return 0


def command_migrate(args: argparse.Namespace) -> int:
    if args.source_schema == "v1" and args.destination_schema == "v2":
        report = migrate_v1_to_v2(
            args.source,
            args.destination,
            migration_epoch_ms=args.migration_epoch_ms,
        )
    elif args.source_schema == "v2" and args.destination_schema == "v1":
        report = migrate_v2_to_v1(
            args.source,
            args.destination,
            migration_epoch_ms=args.migration_epoch_ms,
        )
    else:
        raise DiagError(
            "E_CONFIG",
            "unsupported migration direction",
            field="source_schema",
        )
    if args.report:
        atomic_write_json(args.report, report)
    _emit(report)
    return 0


def command_config_info(args: argparse.Namespace) -> int:
    config: ConfigSnapshot = load_config(args.config)
    _emit(
        {
            "config": config.reference(),
            "enabled_routes": list(config.enabled_routes),
            "normalization": config.fusion["normalization"],
            "dedup_policy": config.fusion["dedup_policy"],
            "candidate_budget": config.fusion["candidate_budget"],
            "worker_count": config.fusion["worker_count"],
            "memory_limit_mb": config.fusion["memory_limit_mb"],
            "tombstones": list(config.tombstones),
            "migration_log": list(config.migration_log),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fusion-diag",
        description="Deterministic recall fusion diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate", help="write deterministic sample files"
    )
    generate_parser.add_argument("--out", required=True)
    generate_parser.add_argument("--seed", type=int, default=20261127)
    generate_parser.set_defaults(handler=command_generate)

    validate_parser = subparsers.add_parser(
        "validate", help="validate and deterministically prepare inputs"
    )
    _workspace_args(validate_parser)
    validate_parser.add_argument("--prepared-out")
    validate_parser.add_argument("--stats-out")
    validate_parser.set_defaults(handler=command_validate)

    batch_parser = subparsers.add_parser(
        "batch", help="batch query processing with automatic incremental reuse"
    )
    _workspace_args(batch_parser)
    batch_parser.add_argument("--workdir", required=True)
    batch_parser.add_argument("--out", required=True)
    batch_parser.add_argument("--mode", choices=("auto", "full"), default="auto")
    batch_parser.add_argument("--report")
    batch_parser.add_argument("--no-cache", action="store_true")
    batch_parser.add_argument("--no-resume", action="store_true")
    batch_parser.add_argument("--no-lock", action="store_true")
    batch_parser.add_argument("--verify-full", action="store_true")
    batch_parser.add_argument("--crash-after", choices=("ingest", "recall", "align", "fusion", "commit"))
    batch_parser.set_defaults(handler=command_batch)

    query_parser = subparsers.add_parser(
        "query", help="interactive query by query_id"
    )
    _workspace_args(query_parser)
    query_parser.add_argument("--workdir", required=True)
    query_parser.add_argument("--query-id", required=True)
    query_parser.add_argument("--repeat", type=int, default=5)
    query_parser.add_argument("--raw", action="store_true")
    query_parser.add_argument("--compare-batch")
    query_parser.add_argument("--no-cache", action="store_true")
    query_parser.add_argument("--no-lock", action="store_true")
    query_parser.set_defaults(handler=command_query)

    migrate_parser = subparsers.add_parser(
        "migrate", help="migrate configuration snapshots between v1 and v2"
    )
    migrate_parser.add_argument("--source", required=True)
    migrate_parser.add_argument("--destination", required=True)
    migrate_parser.add_argument(
        "--source-schema", choices=("v1", "v2"), required=True
    )
    migrate_parser.add_argument(
        "--destination-schema", choices=("v1", "v2"), required=True
    )
    migrate_parser.add_argument(
        "--migration-epoch-ms", type=int, default=1_700_000_000_000
    )
    migrate_parser.add_argument("--report")
    migrate_parser.set_defaults(handler=command_migrate)

    config_parser = subparsers.add_parser(
        "config-info", help="inspect a loaded configuration snapshot"
    )
    config_parser.add_argument("--config", required=True)
    config_parser.set_defaults(handler=command_config_info)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except SimulatedCrash as exc:
        sys.stderr.write(
            canonical_json(
                {
                    "error": "E_SIMULATED_TERMINATION",
                    "stage": exc.stage,
                    "message": str(exc),
                }
            )
            + "\n"
        )
        return 137
    except DiagError as exc:
        sys.stderr.write(exc.to_json() + "\n")
        return 2
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            canonical_json(
                {"error": "E_IO", "message": str(exc)}
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

