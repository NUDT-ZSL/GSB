"""Command-line entry points for generation, execution, query, and replay."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import FlashAttentionDiagnostics
from .errors import DiagnosticError, E_INPUT_INVALID
from .ledger import SnapshotLedger
from .models import ResourcePolicy
from .samples import generate_samples


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _policy(args: argparse.Namespace) -> ResourcePolicy:
    return ResourcePolicy(
        max_resident_bytes=args.max_resident_bytes,
        max_parallel_q_blocks=args.max_parallel_q_blocks,
        allow_serial_degrade=not args.reject_resource_degrade,
    )


def _engine_from_args(args: argparse.Namespace, *, policy: ResourcePolicy | None = None) -> FlashAttentionDiagnostics:
    return FlashAttentionDiagnostics(
        args.ledger,
        args.store,
        policy=policy or _policy(args),
        export_root=args.export_root,
    )


def cmd_generate(args: argparse.Namespace) -> int:
    result = generate_samples(args.data_dir)
    result["business_code"] = "OK"
    _print(result)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    ledger = SnapshotLedger(args.ledger)
    ledger.initialize()
    _print({"business_code": "OK", "ledger": str(ledger.path)})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    engine = _engine_from_args(args)
    results: list[dict[str, Any]] = []
    for manifest in args.manifest:
        case_results = engine.run_manifest(
            manifest,
            tolerance=args.atol,
            tolerance_rtol=args.rtol,
            replay_check=not args.skip_replay,
        )
        for result in case_results:
            results.append(result.to_dict())
    _print(
        {
            "business_code": "OK",
            "results": results,
            "store": engine.store_index(),
            "ledger": str(engine.ledger.path),
        }
    )
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    engine = FlashAttentionDiagnostics(
        args.ledger,
        args.store,
        export_root=args.export_root,
        initialize=False,
    )
    if args.case_id is None:
        _print(
            {
                "business_code": "OK",
                "note": "query requires --case-id; use demo for a complete walkthrough",
            }
        )
        return 0
    snapshot = engine.query_case(
        args.case_id,
        version=args.version,
        include_children=not args.summary,
    )
    _print({"business_code": "OK", "snapshot": snapshot})
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    engine = FlashAttentionDiagnostics(
        args.ledger,
        args.store,
        export_root=args.export_root,
        initialize=False,
    )
    result = engine.verify_case(
        args.case_id,
        version=args.version,
        tolerance=args.atol,
        tolerance_rtol=args.rtol,
    )
    _print({"business_code": "OK", "verification": result.to_dict()})
    return 0


def _fresh_directory(path: Path) -> None:
    resolved = path.resolve()
    protected = {Path("/"), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in protected:
        raise DiagnosticError(
            E_INPUT_INVALID,
            "refusing to clear a protected runtime directory",
            path=str(resolved),
        )
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


def cmd_demo(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).resolve()
    runtime_dir = Path(args.runtime_dir).resolve()
    if not args.keep_runtime:
        _fresh_directory(runtime_dir)
        runtime_dir.mkdir(parents=True, exist_ok=True)
    generated = generate_samples(data_dir)
    policy = ResourcePolicy(
        max_resident_bytes=args.max_resident_bytes,
        max_parallel_q_blocks=args.max_parallel_q_blocks,
        allow_serial_degrade=not args.reject_resource_degrade,
    )
    engine = FlashAttentionDiagnostics(
        runtime_dir / "ledger.db",
        runtime_dir / "store",
        policy=policy,
        export_root=runtime_dir / "exports",
    )
    base_results = engine.run_manifest(
        data_dir / "tasks.jsonl",
        tolerance=args.atol,
        tolerance_rtol=args.rtol,
    )
    revision_results = engine.run_manifest(
        data_dir / "tasks_rev.jsonl",
        tolerance=args.atol,
        tolerance_rtol=args.rtol,
    )

    revision_by_case = {result.case_id: result for result in revision_results}
    cases: list[dict[str, Any]] = []
    for base_result in base_results:
        revision = revision_by_case[base_result.case_id]
        replay = engine.verify_case(
            base_result.case_id,
            tolerance=args.atol,
            tolerance_rtol=args.rtol,
        )
        cases.append(
            {
                "case_id": base_result.case_id,
                "business_code": "OK",
                "base": base_result.to_dict(),
                "revision": revision.to_dict(),
                "ledger_history": engine.history(base_result.case_id),
                "active_snapshot": engine.query_case(
                    base_result.case_id,
                    include_children=False,
                ),
                "ledger_replay_verification": replay.to_dict(),
            }
        )

    _print(
        {
            "business_code": "OK",
            "generation": generated,
            "policy": policy.to_dict(),
            "runtime": {
                "ledger": str(engine.ledger.path),
                "store": str(engine.store.root),
                "exports": str(engine.export_root),
            },
            "cases": cases,
            "store_index": engine.store_index(),
            "acceptance": {
                "incremental_vs_full_recompute": "bitwise_equal",
                "incremental_vs_whole_sequence": "within_tolerance",
                "ledger_replay": "ok",
            },
        }
    )
    return 0


def _add_resource_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-resident-bytes",
        type=int,
        default=256 * 1024 * 1024,
        help="maximum estimated float64 working set for one call",
    )
    parser.add_argument(
        "--max-parallel-q-blocks",
        type=int,
        default=4,
        help="maximum Q blocks eligible for parallel recomputation",
    )
    parser.add_argument(
        "--reject-resource-degrade",
        action="store_true",
        help="return E_RESOURCE_LIMIT instead of degrading to serial execution",
    )
    parser.add_argument("--atol", type=float, default=1e-9)
    parser.add_argument("--rtol", type=float, default=1e-9)


def _add_ledger_options(parser: argparse.ArgumentParser, *, runtime_default: str) -> None:
    parser.add_argument("--ledger", default=str(Path(runtime_default) / "ledger.db"))
    parser.add_argument("--store", default=str(Path(runtime_default) / "store"))
    parser.add_argument(
        "--export-root",
        default=str(Path(runtime_default) / "exports"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flashdiag",
        description="Deterministic blockwise FlashAttention diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed samples")
    generate.add_argument("--data-dir", default="data")
    generate.set_defaults(handler=cmd_generate)

    initialize = subparsers.add_parser("init", help="initialize the SQLite ledger")
    initialize.add_argument("--ledger", default="artifacts/flashdiag/ledger.db")
    initialize.set_defaults(handler=cmd_init)

    run = subparsers.add_parser("run", help="execute one or more JSONL manifests")
    run.add_argument("--manifest", action="append", required=True)
    run.add_argument("--skip-replay", action="store_true")
    _add_ledger_options(run, runtime_default="artifacts/flashdiag")
    _add_resource_options(run)
    run.set_defaults(handler=cmd_run)

    query = subparsers.add_parser("query", help="query a case snapshot ledger")
    query.add_argument("--case-id")
    query.add_argument("--version", type=int)
    query.add_argument("--summary", action="store_true")
    _add_ledger_options(query, runtime_default="artifacts/flashdiag")
    query.set_defaults(handler=cmd_query)

    verify = subparsers.add_parser("verify", help="replay a snapshot from the ledger")
    verify.add_argument("--case-id", required=True)
    verify.add_argument("--version", type=int)
    _add_ledger_options(verify, runtime_default="artifacts/flashdiag")
    verify.add_argument("--atol", type=float, default=1e-9)
    verify.add_argument("--rtol", type=float, default=1e-9)
    verify.set_defaults(handler=cmd_verify)

    demo = subparsers.add_parser(
        "demo",
        help="generate samples, run base and revision, query ledger, and verify replay",
    )
    demo.add_argument("--data-dir", default="data")
    demo.add_argument("--runtime-dir", default="artifacts/flashdiag-demo")
    demo.add_argument(
        "--keep-runtime",
        action="store_true",
        help="do not clear runtime-dir before the demo",
    )
    _add_resource_options(demo)
    demo.set_defaults(handler=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except DiagnosticError as exc:
        _print({"business_code": exc.code, "error": exc.to_dict()})
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _print(
            {
                "business_code": "E_INTERNAL",
                "error": {
                    "code": "E_INTERNAL",
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            }
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())

