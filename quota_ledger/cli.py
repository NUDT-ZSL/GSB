from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from quota_ledger.db import Store
from quota_ledger.engine import adjust_pending, adjust_round, resume_pipeline, run_pipeline
from quota_ledger.errors import BusinessError
from quota_ledger.loader import load_into_store
from quota_ledger.reporting import status_report, verify_report
from quota_ledger.validation import load_dataset


def _emit(payload: Any) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
    )


def _require_loaded(store: Store, dataset_digest: str) -> None:
    if not store.is_initialized():
        raise BusinessError(
            "E_STATE_NOT_INITIALIZED",
            "database is not initialized; run load first",
            str(store.path),
        )
    with store.read_conn() as connection:
        loaded = store.meta_get(connection, "dataset_digest")
    if loaded is None:
        raise BusinessError(
            "E_STATE_NOT_INITIALIZED",
            "database has no loaded dataset; run load first",
            str(store.path),
        )
    if loaded != dataset_digest:
        raise BusinessError(
            "E_DATASET_CHANGED",
            "input data digest differs from the loaded state; run load with --force "
            "or use a new database",
            str(store.path),
            loaded=loaded,
            incoming=dataset_digest,
        )


def _add_common(parser: argparse.ArgumentParser, *, data_required: bool = True) -> None:
    if data_required:
        parser.add_argument(
            "--data-dir",
            default="sample",
            help="directory containing deterministic JSON/SQLite samples",
        )
    parser.add_argument(
        "--db",
        default="quota_ledger.db",
        help="SQLite state database path",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quota-ledger",
        description="Lease-quota ledger, drift threshold scheduler, and recovery CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate input and initialize state")
    _add_common(load_parser)
    load_parser.add_argument(
        "--force",
        action="store_true",
        help="replace a database loaded with a different dataset (before processing)",
    )

    run_parser = subparsers.add_parser("run", help="process application/release/observation rounds")
    _add_common(run_parser)
    run_parser.add_argument("--until-round", type=int, default=None)
    run_parser.add_argument(
        "--no-adjust",
        action="store_true",
        help="process events but leave threshold scheduling to the adjust command",
    )

    adjust_parser = subparsers.add_parser("adjust", help="apply pending fair threshold scheduling")
    _add_common(adjust_parser)
    adjust_parser.add_argument(
        "--round",
        type=int,
        default=None,
        help="adjust a specific next round; default adjusts all pending rounds",
    )

    resume_parser = subparsers.add_parser("resume", help="resume from committed SQLite watermarks")
    _add_common(resume_parser)
    resume_parser.add_argument("--until-round", type=int, default=None)

    status_parser = subparsers.add_parser("status", help="emit stable diagnostic JSON")
    _add_common(status_parser, data_required=False)

    verify_parser = subparsers.add_parser("verify", help="check ledger and scheduling invariants")
    _add_common(verify_parser, data_required=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        if args.command == "load":
            dataset = load_dataset(args.data_dir)
            result = load_into_store(dataset, store, force=args.force)
            _emit(result)
            return 0
        if args.command == "run":
            dataset = load_dataset(args.data_dir)
            _require_loaded(store, dataset.digest)
            result = run_pipeline(
                store,
                until_round=args.until_round,
                no_adjust=args.no_adjust,
            )
            result["dataset_digest"] = dataset.digest
            _emit(result)
            return 0
        if args.command == "adjust":
            dataset = load_dataset(args.data_dir)
            _require_loaded(store, dataset.digest)
            if args.round is not None:
                result = adjust_round(store, args.round)
            else:
                pending = adjust_pending(store)
                result = {
                    "ok": True,
                    "adjusted": pending,
                }
            result["dataset_digest"] = dataset.digest
            _emit(result)
            return 0
        if args.command == "resume":
            dataset = load_dataset(args.data_dir)
            _require_loaded(store, dataset.digest)
            result = resume_pipeline(store, until_round=args.until_round)
            result["dataset_digest"] = dataset.digest
            _emit(result)
            return 0
        if args.command == "status":
            result = status_report(store)
            _emit(result)
            return 0
        if args.command == "verify":
            result = verify_report(store)
            _emit(result)
            return 0 if result["ok"] else 3
        parser.error(f"unknown command: {args.command}")
    except BusinessError as exc:
        _emit({"ok": False, "error": exc.as_dict()})
        return 2
    except KeyboardInterrupt:
        _emit({"ok": False, "error": {"code": "E_INTERRUPTED", "message": "interrupted"}})
        return 130
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        payload = {
            "ok": False,
            "error": {
                "code": "E_INTERNAL",
                "message": str(exc),
                "type": type(exc).__name__,
            },
        }
        _emit(payload)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

