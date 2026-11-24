"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .codes import EXIT_OK, EXIT_PARAMETER, EXIT_PARTIAL, E_INTERNAL
from .engine import RunOptions, run
from .generator import generate_fixture
from .util import GuardError, LockUnavailable


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memguard")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="write deterministic local fixtures")
    generate.add_argument("--out-dir", required=True, type=Path)
    generate.add_argument("--seed", type=int, default=20261125)

    execute = subparsers.add_parser("run", help="evaluate traces against the ledger")
    execute.add_argument("--regions", action="append", required=True, type=Path)
    execute.add_argument("--trace", action="append", required=True, type=Path)
    execute.add_argument("--database", required=True, type=Path)
    execute.add_argument("--snapshot", type=Path)
    execute.add_argument("--report", required=True, type=Path)
    execute.add_argument("--truth-markers", action="append", type=Path, default=[])
    execute.add_argument("--workers", type=int, default=4)
    execute.add_argument("--max-inflight-bytes", type=int, default=32 * 1024 * 1024)
    execute.add_argument("--arena-timeout-ms", type=float, default=5000.0)
    execute.add_argument("--max-pending", type=int, default=64)
    execute.add_argument("--batch-size", type=int, default=256)
    return parser


def _error_payload(code: str, message: str, detail: object = None) -> dict[str, object]:
    return {
        "status": "error",
        "business_code": code,
        "message": message,
        "detail": detail if detail is not None else {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "generate":
        try:
            paths = generate_fixture(args.out_dir, args.seed)
        except (OSError, ValueError) as exc:
            print(json.dumps(_error_payload(E_INTERNAL, str(exc)), ensure_ascii=False, sort_keys=True))
            return EXIT_PARTIAL
        print(json.dumps({"status": "ok", "files": paths}, ensure_ascii=False, sort_keys=True))
        return EXIT_OK

    try:
        result = run(
            RunOptions(
                region_paths=args.regions,
                trace_paths=args.trace,
                database_path=args.database,
                report_path=args.report,
                snapshot_path=args.snapshot,
                truth_marker_paths=args.truth_markers,
                max_workers=args.workers,
                max_inflight_bytes=args.max_inflight_bytes,
                arena_timeout_ms=args.arena_timeout_ms,
                max_pending=args.max_pending,
                batch_size=args.batch_size,
            )
        )
    except LockUnavailable as exc:
        print(
            json.dumps(
                _error_payload(exc.business_code, exc.message, exc.detail),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return EXIT_PARTIAL
    except GuardError as exc:
        print(
            json.dumps(
                _error_payload(exc.business_code, exc.message, exc.detail),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return EXIT_PARAMETER if exc.business_code == "E_CONFIG" else EXIT_PARTIAL
    except OSError as exc:
        print(
            json.dumps(
                _error_payload(E_INTERNAL, str(exc)),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return EXIT_PARTIAL
    except Exception as exc:
        print(
            json.dumps(
                _error_payload(E_INTERNAL, repr(exc)),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return EXIT_PARTIAL

    summary = {
        "status": "ok" if result.exit_code == EXIT_OK else "partial",
        "batch_status": result.report["batch"]["status"],
        "business_codes": result.report["batch"]["business_codes"],
        "checkpoint_version": result.report["ledger"]["checkpoint"].get(
            "checkpoint_version"
        ),
        "report": str(args.report),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
