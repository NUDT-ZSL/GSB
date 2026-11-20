"""Deterministic command-line interface."""

from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from .canonical import canonical_json
from .errors import BusinessError, E_BAD_OP, E_IO
from .service import WorkspaceService


class BusinessArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BusinessError(
            E_BAD_OP,
            "invalid command-line arguments",
            {"argument_error": message},
        )


def build_parser() -> argparse.ArgumentParser:
    parser = BusinessArgumentParser(
        prog="index-maintenance",
        description=(
            "Deterministic B+ tree page-chain maintenance and secondary-index "
            "rebuild adjudication."
        ),
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="workspace containing .index_maint/index.sqlite3 (default: current directory)",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=30.0,
        help="seconds to wait for the workspace file lock",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load a JSON sample")
    load_parser.add_argument("--sample", required=True)
    load_parser.add_argument("--reset", action="store_true")

    apply_parser = subparsers.add_parser(
        "apply",
        help="apply a contiguous or non-overlapping operation range",
    )
    apply_parser.add_argument("--sample")
    apply_parser.add_argument("--from-seq", type=int)
    apply_parser.add_argument("--to-seq", type=int)
    apply_parser.add_argument("--wait-timeout", type=float, default=30.0)
    apply_parser.add_argument(
        "--crash-after-split",
        type=int,
        help="test hook: terminate the process after N staged page splits",
    )
    apply_parser.add_argument(
        "--crash-after-secondary",
        type=int,
        help="test hook: terminate after N staged secondary-index updates",
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="discard staged attempts and replay unfinished committed-log operations",
    )
    resume_parser.add_argument("--sample")
    resume_parser.add_argument("--crash-after-split", type=int)
    resume_parser.add_argument("--crash-after-secondary", type=int)

    rebuild_parser = subparsers.add_parser(
        "rebuild",
        help="force a deterministic full rebuild of one or all secondary indexes",
    )
    rebuild_parser.add_argument("--sample")
    group = rebuild_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--index")
    group.add_argument("--all", action="store_true")

    status_parser = subparsers.add_parser(
        "status",
        help="emit stable JSON status for every index",
    )
    status_parser.add_argument("--sample")

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify persisted digests, page chains, replay, and ledger consistency",
    )
    verify_parser.add_argument("--sample")
    verify_parser.add_argument("--expected-ledger-version", type=int)
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    service = WorkspaceService(args.workspace, lock_timeout=args.lock_timeout)
    if args.command == "load":
        return service.load(args.sample, reset=args.reset)
    if args.command == "apply":
        return service.apply(
            sample_path=args.sample,
            from_seq=args.from_seq,
            to_seq=args.to_seq,
            crash_after_split=args.crash_after_split,
            crash_after_secondary=args.crash_after_secondary,
            wait_timeout=args.wait_timeout,
        )
    if args.command == "resume":
        return service.resume(
            sample_path=args.sample,
            crash_after_split=args.crash_after_split,
            crash_after_secondary=args.crash_after_secondary,
        )
    if args.command == "rebuild":
        return service.rebuild(
            index_name=args.index,
            rebuild_all=args.all,
            sample_path=args.sample,
        )
    if args.command == "status":
        return service.status(sample_path=args.sample)
    if args.command == "verify":
        return service.verify(
            sample_path=args.sample,
            expected_ledger_version=args.expected_ledger_version,
        )
    raise BusinessError(E_BAD_OP, "unknown command", {"command": args.command})


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(canonical_json(payload) + "\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        emit(dispatch(args))
        return 0
    except BusinessError as exc:
        emit({"ok": False, "error": exc.to_dict()})
        return 2
    except OSError as exc:
        error = BusinessError(
            E_IO,
            "workspace I/O operation failed",
            {"reason": exc.strerror or str(exc)},
        )
        emit({"ok": False, "error": error.to_dict()})
        return 2
    except Exception as exc:
        error = {
            "code": "E_INTERNAL",
            "message": "unexpected internal failure",
            "details": {
                "exception_type": type(exc).__name__,
                "reason": str(exc),
            },
        }
        emit({"ok": False, "error": error})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
