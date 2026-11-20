from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from .engine import Engine, application_hash, initialize_database
from .errors import (
    E_INVALID_APPLICATION,
    E_SAMPLE_DIGEST_MISMATCH,
    E_STATE_INCONSISTENT,
    PoolError,
)
from .model import Application
from .replay import replay_catalog
from .storage import Database, canonical_json
from .transport import LoopbackTransport, SimulatedTransport
from .validation import load_catalog


def _emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    sys.stdout.write("\n")


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db)


def _load_trace_application(
    db_path: Path,
    seq: int,
    version: int,
) -> dict[str, Any] | None:
    db = Database(db_path)
    try:
        row = db.fetchone(
            """
            SELECT * FROM trace_applications
            WHERE app_seq = ? AND app_version = ?
            """,
            (seq, version),
        )
        return dict(row) if row is not None else None
    finally:
        db.close()


def command_load(args: argparse.Namespace) -> dict[str, Any]:
    catalog = load_catalog(args.endpoints, args.config, args.trace)
    if args.expect_digest is not None and args.expect_digest != catalog.sample_digest:
        raise PoolError(
            E_SAMPLE_DIGEST_MISMATCH,
            "sample digest does not match --expect-digest",
            operation="load",
            expected=args.expect_digest,
            actual=catalog.sample_digest,
        )
    result = initialize_database(
        _db_path(args),
        catalog,
        expected_digest=args.expect_digest,
    )
    result["sample_digest"] = catalog.sample_digest
    result["artifacts"] = {
        "endpoints_digest": catalog.endpoint_digest,
        "config_digest": catalog.config_digest,
        "trace_digest": catalog.trace_digest,
    }
    return result


def command_acquire(args: argparse.Namespace) -> dict[str, Any]:
    db_path = _db_path(args)
    row = _load_trace_application(db_path, args.seq, args.version)
    if row is not None:
        application = Application(
            seq=int(row["app_seq"]),
            version=int(row["app_version"]),
            tenant=str(row["tenant"]),
            pool_label=str(row["pool_label"]),
            apply_clock_ms=int(row["apply_clock_ms"]),
            hold_ms=int(row["hold_ms"]),
            release=bool(row["release_flag"]),
        )
    else:
        missing = [
            name
            for name, value in (
                ("--tenant", args.tenant),
                ("--pool-label", args.pool_label),
                ("--hold-ms", args.hold_ms),
                ("--apply-clock-ms", args.apply_clock_ms),
            )
            if value is None
        ]
        if missing:
            raise PoolError(
                E_INVALID_APPLICATION,
                "application is not in the loaded trace and explicit fields are missing",
                operation="acquire",
                seq=args.seq,
                missing=missing,
            )
        application = Application(
            seq=args.seq,
            version=args.version,
            tenant=args.tenant,
            pool_label=args.pool_label,
            apply_clock_ms=args.apply_clock_ms,
            hold_ms=args.hold_ms,
            release=not args.keep_active,
        )
    with Engine(db_path) as engine:
        result = engine.acquire(application, args.clock_ms)
        result["application_hash"] = application_hash(application)
        return result


def command_release(args: argparse.Namespace) -> dict[str, Any]:
    with Engine(_db_path(args)) as engine:
        return engine.release(
            args.seq,
            args.version,
            args.clock_ms,
            close=args.close,
            reason=args.reason,
        )


def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    with Engine(_db_path(args)) as engine:
        return engine.resume(now_ms=args.clock_ms)


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    with Engine(_db_path(args)) as engine:
        return engine.status(at_clock_ms=args.at_clock)


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    catalog = load_catalog(args.endpoints, args.config, args.trace)
    if args.expect_digest is not None and args.expect_digest != catalog.sample_digest:
        raise PoolError(
            E_SAMPLE_DIGEST_MISMATCH,
            "sample digest does not match --expect-digest",
            operation="verify",
            expected=args.expect_digest,
            actual=catalog.sample_digest,
        )
    if args.transport == "simulated":
        factory = SimulatedTransport
    else:
        factory = LoopbackTransport

    with tempfile.TemporaryDirectory(prefix="dbpool-verify-") as temp:
        root = Path(temp)
        auto_one = replay_catalog(
            root / "auto-one.db",
            catalog,
            mode=None,
            transport_factory=factory,
        )
        auto_two = replay_catalog(
            root / "auto-two.db",
            catalog,
            mode=None,
            transport_factory=factory,
        )
        exact = replay_catalog(
            root / "exact.db",
            catalog,
            mode="exact",
            transport_factory=factory,
        )
        tiered = replay_catalog(
            root / "tiered.db",
            catalog,
            mode="tiered",
            transport_factory=factory,
        )

    auto_one_bytes = canonical_json(auto_one["status"])
    auto_two_bytes = canonical_json(auto_two["status"])
    deterministic = auto_one_bytes == auto_two_bytes
    exact_tiered_equivalent = (
        canonical_json(exact["leases_issued"])
        == canonical_json(tiered["leases_issued"])
    )
    if not deterministic:
        raise PoolError(
            E_STATE_INCONSISTENT,
            "two identical replays produced different states",
            operation="verify",
        )
    if not exact_tiered_equivalent:
        raise PoolError(
            E_STATE_INCONSISTENT,
            "exact and tiered schedulers produced different lease sequences",
            operation="verify",
        )
    if auto_one["leases_issued"] != auto_two["leases_issued"]:
        raise PoolError(
            E_STATE_INCONSISTENT,
            "two identical replays produced different lease sequences",
            operation="verify",
        )
    if auto_one["leases_issued"] != exact["leases_issued"]:
        raise PoolError(
            E_STATE_INCONSISTENT,
            "adaptive switching changed the lease issuance sequence",
            operation="verify",
        )
    status = auto_one["status"]
    return {
        "ok": True,
        "deterministic": deterministic,
        "exact_tiered_equivalent": exact_tiered_equivalent,
        "sample_digest": catalog.sample_digest,
        "state_sha256": status["state_digest"],
        "leases_issued": auto_one["leases_issued"],
        "closed_events": auto_one["closed_events"],
        "mode_switches": status["algorithm"]["switches"],
        "status": status,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbpool",
        description="Deterministic local database connection-pool engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate samples and initialize the pool")
    load.add_argument("--endpoints", required=True)
    load.add_argument("--config", required=True)
    load.add_argument("--trace", required=True)
    load.add_argument("--db", required=True)
    load.add_argument("--expect-digest")
    load.set_defaults(func=command_load)

    acquire = subparsers.add_parser("acquire", help="acquire or queue an application")
    acquire.add_argument("--db", required=True)
    acquire.add_argument("--seq", required=True, type=int)
    acquire.add_argument("--version", type=int, default=1)
    acquire.add_argument("--clock-ms", required=True, type=int)
    acquire.add_argument("--tenant")
    acquire.add_argument("--pool-label")
    acquire.add_argument("--hold-ms", type=int)
    acquire.add_argument("--apply-clock-ms", type=int)
    acquire.add_argument("--keep-active", action="store_true")
    acquire.set_defaults(func=command_acquire)

    release = subparsers.add_parser("release", help="release an active lease")
    release.add_argument("--db", required=True)
    release.add_argument("--seq", required=True, type=int)
    release.add_argument("--version", type=int, default=1)
    release.add_argument("--clock-ms", required=True, type=int)
    release.add_argument("--close", action="store_true")
    release.add_argument("--reason", default="HOLD_EXPIRED")
    release.set_defaults(func=command_release)

    resume = subparsers.add_parser("resume", help="repair and recover committed state")
    resume.add_argument("--db", required=True)
    resume.add_argument("--clock-ms", type=int)
    resume.set_defaults(func=command_resume)

    status = subparsers.add_parser("status", help="print stable pool state JSON")
    status.add_argument("--db", required=True)
    status.add_argument("--at-clock", type=int)
    status.set_defaults(func=command_status)

    verify = subparsers.add_parser(
        "verify",
        help="run deterministic and exact/tiered-equivalence verification",
    )
    verify.add_argument("--endpoints", required=True)
    verify.add_argument("--config", required=True)
    verify.add_argument("--trace", required=True)
    verify.add_argument("--expect-digest")
    verify.add_argument(
        "--transport",
        choices=["loopback", "simulated"],
        default="loopback",
    )
    verify.set_defaults(func=command_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except PoolError as exc:
        sys.stderr.write(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        sys.stderr.write("\n")
        return 2
    except KeyboardInterrupt:
        return 130
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
