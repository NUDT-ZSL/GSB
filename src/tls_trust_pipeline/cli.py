from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .audit import verify_audit
from .canonical import pretty_json
from .engine import PipelineEngine, parse_requests
from .errors import E_AUDIT_FAILED, E_BAD_REQUEST, PipelineError
from .store import Store


def _add_common(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default_db = argparse.SUPPRESS if suppress_defaults else Path("var/state.sqlite3")
    default_lock = argparse.SUPPRESS if suppress_defaults else None
    default_timeout = argparse.SUPPRESS if suppress_defaults else 20.0
    parser.add_argument(
        "--db",
        type=Path,
        default=default_db,
        help="SQLite state database (default: var/state.sqlite3)",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=default_lock,
        help="advisory lock file (default: <db>.lock)",
    )
    parser.add_argument(
        "--lock-timeout-seconds",
        type=float,
        default=default_timeout,
        help="bounded file-lock wait",
    )


def _add_validation_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cert-dir", type=Path, required=True)
    parser.add_argument("--revocation-evidence", type=Path, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tls-trust",
        description="Deterministic local TLS chain validation and trust-store pipeline",
    )
    _add_common(parser)
    parser.add_argument(
        "--clock-tolerance-ms",
        type=int,
        default=300_000,
        help="permitted logical clock rollback in milliseconds",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    default_clock_tolerance = argparse.SUPPRESS

    load = subparsers.add_parser("load", help="validate and atomically load snapshots")
    _add_common(load, suppress_defaults=True)
    load.add_argument(
        "--clock-tolerance-ms", type=int, default=default_clock_tolerance
    )
    load.add_argument("--v1", type=Path, default=None)
    load.add_argument("--v2", type=Path, default=None)
    load.add_argument("--cert-dir", type=Path, required=True)
    load.add_argument("--logical-clock-ms", type=int, required=True)

    chain = subparsers.add_parser("chain", help="build and trace chains")
    _add_common(chain, suppress_defaults=True)
    chain.add_argument(
        "--clock-tolerance-ms", type=int, default=default_clock_tolerance
    )
    _add_validation_paths(chain)
    chain.add_argument("--requests", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="verify full handshakes and cache conclusions")
    _add_common(verify, suppress_defaults=True)
    verify.add_argument(
        "--clock-tolerance-ms", type=int, default=default_clock_tolerance
    )
    _add_validation_paths(verify)
    verify.add_argument("--requests", type=Path, required=True)

    apply_parser = subparsers.add_parser("apply", help="apply an incremental trust patch")
    _add_common(apply_parser, suppress_defaults=True)
    apply_parser.add_argument(
        "--clock-tolerance-ms", type=int, default=default_clock_tolerance
    )
    apply_parser.add_argument("--patch", type=Path, required=True)
    apply_parser.add_argument("--cert-dir", type=Path, required=True)
    apply_parser.add_argument("--logical-clock-ms", type=int, required=True)

    resume = subparsers.add_parser("resume", help="replay unfinished jobs only")
    _add_common(resume, suppress_defaults=True)
    resume.add_argument(
        "--clock-tolerance-ms", type=int, default=default_clock_tolerance
    )
    _add_validation_paths(resume)

    status = subparsers.add_parser("status", help="show current version and invalidation counts")
    _add_common(status, suppress_defaults=True)
    status.add_argument(
        "--clock-tolerance-ms", type=int, default=default_clock_tolerance
    )
    audit_parser = subparsers.add_parser(
        "verify-audit", help="verify persisted conclusions and invalidations"
    )
    _add_common(audit_parser, suppress_defaults=True)
    audit_parser.add_argument(
        "--clock-tolerance-ms", type=int, default=default_clock_tolerance
    )
    return parser


def _store_and_engine(args: argparse.Namespace) -> tuple[Store, PipelineEngine | None]:
    store = Store(
        args.db,
        args.lock,
        lock_timeout_seconds=args.lock_timeout_seconds,
    )
    engine = None
    if hasattr(args, "cert_dir"):
        engine = PipelineEngine(
            store,
            cert_dir=args.cert_dir,
            revocation_evidence_path=getattr(args, "revocation_evidence", None),
            clock_tolerance_ms=args.clock_tolerance_ms,
        )
    return store, engine


def run(args: argparse.Namespace) -> dict:
    store, engine = _store_and_engine(args)
    if args.command == "load":
        assert engine is not None
        return engine.load(
            v1_path=args.v1,
            v2_path=args.v2,
            logical_clock_ms=args.logical_clock_ms,
        )
    if args.command in ("chain", "verify"):
        assert engine is not None
        requests = parse_requests(args.requests)
        return engine.evaluate_requests(requests, mode=args.command)
    if args.command == "apply":
        assert engine is not None
        return engine.apply_patch_file(
            args.patch,
            logical_clock_ms=args.logical_clock_ms,
        )
    if args.command == "resume":
        assert engine is not None
        return engine.resume()
    if args.command == "status":
        if not args.db.exists():
            return {
                "ok": True,
                "initialized": False,
                "current_version": None,
                "current_generation": None,
                "anchor_count": 0,
                "counts": {},
            }
        result = store.status()
        result["ok"] = True
        return result
    if args.command == "verify-audit":
        if not args.db.exists():
            raise PipelineError(E_BAD_REQUEST, "state database does not exist")
        return verify_audit(store)
    raise PipelineError(E_BAD_REQUEST, "unknown command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        result = run(args)
        sys.stdout.write(pretty_json(result) + "\n")
        return 0
    except PipelineError as exc:
        sys.stderr.write(pretty_json(exc.as_dict()) + "\n")
        return 3 if exc.code == E_AUDIT_FAILED else 2
    except KeyboardInterrupt:
        sys.stderr.write(pretty_json({"ok": False, "error": {"code": "E_INTERRUPTED"}}) + "\n")
        return 130
    except Exception as exc:
        sys.stderr.write(
            pretty_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
            )
            + "\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

