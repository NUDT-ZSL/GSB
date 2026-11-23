"""Command line interface with deterministic, stable-sorted JSON output."""

from __future__ import annotations

import argparse
import json
import sys

from .errors import DiagError
from .engine import DiagnosticEngine, SampleLayout, build_trust_view
from .samples import generate_sample
from .storage import Storage
from .truststore import load_v1, load_v2


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _engine(args) -> tuple[SampleLayout, Storage, DiagnosticEngine]:
    layout = SampleLayout.load(args.sample)
    storage = Storage(args.db)
    return layout, storage, DiagnosticEngine(layout, storage)


def cmd_gensamples(args) -> int:
    _print(generate_sample(args.sample))
    return 0


def cmd_load(args) -> int:
    layout = SampleLayout.load(args.sample)
    v1 = load_v1(layout.v1_path)
    v2 = load_v2(layout.v2_manifest)
    _print(
        {
            "v1": {
                "anchors": sorted(anchor.alias for anchor in v1.anchors),
                "logical_clock_tick": v1.logical_clock_tick,
            },
            "v2": {
                "anchors": sorted(anchor.alias for anchor in v2.anchors),
                "logical_clock_tick": v2.logical_clock_tick,
            },
        }
    )
    return 0


def cmd_apply(args) -> int:
    layout, storage, engine = _engine(args)
    try:
        result = engine.apply_snapshot(args.version)
        _print(result)
    finally:
        storage.close()
    return 0


def cmd_chain(args) -> int:
    layout = SampleLayout.load(args.sample)
    view, _, _ = build_trust_view(layout)
    _print(
        {
            "arbitration": view.arbitration_log,
            "anchors": [anchor.to_dict() for _, anchor in sorted(view.anchors.items())],
            "precision_ledger": view.ledger.to_dict(),
        }
    )
    return 0


def cmd_verify(args) -> int:
    layout, storage, engine = _engine(args)
    try:
        if storage.current_version() is None:
            raise DiagError("E3003", "no committed snapshot; run apply first")
        results = engine.verify_all()
        _print({"results": results, "sensitive": storage.sensitive_records()})
    finally:
        storage.close()
    return 0


def cmd_resume(args) -> int:
    layout, storage, engine = _engine(args)
    try:
        _print(engine.resume())
    finally:
        storage.close()
    return 0


def cmd_status(args) -> int:
    _, storage, _ = _engine(args)
    try:
        _print(_status_payload(storage))
    finally:
        storage.close()
    return 0


def _status_payload(storage: Storage) -> dict:
    version = storage.current_version()
    return {
        "current_version": version,
        "clock_high_water_ms": storage.clock_high_water(),
        "chain_conclusion_summary": storage.chain_conclusions(version),
        "precision_sensitive": storage.sensitive_records(version),
        "revocation": storage.revocation_conclusions(),
    }


def cmd_verify_audit(args) -> int:
    _, storage, _ = _engine(args)
    try:
        version = storage.current_version()
        payload = {
            "current_version": version,
            "ledger": storage.ledger_entries(version),
            "precision_sensitive": storage.sensitive_records(version),
            "chain_conclusions": storage.chain_conclusions(version),
            "revocation": storage.revocation_conclusions(),
            "unfinished_jobs": storage.unfinished_jobs(),
        }
        _print(payload)
    finally:
        storage.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tlsdiag", description="Local TLS chain & trust-time precision engine")
    parser.add_argument("--sample", default="sample", help="sample bundle directory (relative path)")
    parser.add_argument("--db", default="work/engine.sqlite3", help="SQLite persistence path (relative path)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gensamples", help="deterministically generate the local sample bundle")
    p.set_defaults(func=cmd_gensamples)

    p = sub.add_parser("load", help="parse and validate both trust store snapshots")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("apply", help="merge snapshots and atomically commit a version")
    p.add_argument("--version", type=int, default=1)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("chain", help="print unified trust view, arbitration and precision ledger")
    p.set_defaults(func=cmd_chain)

    p = sub.add_parser("verify", help="build/validate chains and revocation for every leaf")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("resume", help="replay only unfinished verification jobs after a crash")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("status", help="stable-sorted JSON status summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("verify-audit", help="full precision + chain audit trail")
    p.set_defaults(func=cmd_verify_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DiagError as exc:
        _print({"error": exc.to_dict()})
        return 2
    except FileNotFoundError as exc:
        _print({"error": {"code": "E3003", "message": "required file missing", "detail": str(exc)}})
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
