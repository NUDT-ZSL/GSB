"""Argparse command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .atomicio import atomic_write_json, read_json
from .checkpointing import verify_checkpoint, write_checkpoint
from .codes import Code, PARAMETER_EXIT, PARTIAL_EXIT, SUCCESS_EXIT
from .loader import load_dataset, load_truth
from .models import rejection_exit_code
from .seed import write_sample_workspace
from .service import import_dataset
from .store import Store
from .verifier import verify_rejection_codes, verify_truth


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _db_exists(path: str) -> bool:
    return path != ":memory:" and Path(path).exists()


def _missing_db_result(path: str) -> dict[str, Any]:
    return {"code": Code.DB_NOT_FOUND, "message": f"SQLite database does not exist: {path}"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sensitivity_engine",
        description="Deterministic sensitivity synthesis and re-entry ledger",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-sample", help="generate fixed deterministic data and truth")
    generate.add_argument("--root", default=".")
    generate.add_argument("--data-dir", default="data")
    generate.add_argument("--truth-file", default="truth/expected.json")
    generate.set_defaults(handler=cmd_generate_sample)

    importer = sub.add_parser("import", help="import objects/sources/batches into SQLite")
    importer.add_argument("--data", required=True)
    importer.add_argument("--db", default="data/ledger.sqlite3")
    importer.add_argument("--report", default="reports/import-report.json")
    importer.add_argument("--checkpoint", default="checkpoints/latest.json")
    importer.add_argument("--concurrency", type=int, default=1)
    importer.add_argument("--queue-limit", type=int, default=64)
    importer.add_argument("--batch-timeout-ms", type=float, default=5000.0)
    importer.set_defaults(handler=cmd_import)

    query = sub.add_parser("query", help="query effective levels")
    query.add_argument("--db", default="data/ledger.sqlite3")
    query.add_argument("--object", dest="object_key")
    query.add_argument("--as-of", type=int)
    query.add_argument("--format", choices=("json", "table"), default="json")
    query.set_defaults(handler=cmd_query)

    export = sub.add_parser(
        "export-reentry", aliases=["export"], help="atomically export re-entry report"
    )
    export.add_argument("--db", default="data/ledger.sqlite3")
    export.add_argument("--out", default="reports/reentry-report.json")
    export.set_defaults(handler=cmd_export_reentry)

    check = sub.add_parser(
        "verify-checkpoint", aliases=["checkpoint"], help="verify SQLite and snapshot checkpoint"
    )
    check.add_argument("--db", default="data/ledger.sqlite3")
    check.add_argument("--snapshot", default="checkpoints/latest.json")
    check.set_defaults(handler=cmd_verify_checkpoint)

    truth = sub.add_parser("verify-truth", help="compare ledger output with fixed truth")
    truth.add_argument("--db", default="data/ledger.sqlite3")
    truth.add_argument("--truth", default="truth/expected.json")
    truth.add_argument("--data")
    truth.add_argument("--out", default="reports/truth-verification.json")
    truth.set_defaults(handler=cmd_verify_truth)

    recover = sub.add_parser("recover", help="recover to last complete checkpoint and replay")
    recover.add_argument("--data", required=True)
    recover.add_argument("--db", default="data/ledger.sqlite3")
    recover.add_argument("--snapshot", default="checkpoints/latest.json")
    recover.add_argument("--out", default="reports/recovery-report.json")
    recover.set_defaults(handler=cmd_recover)
    return parser


def cmd_generate_sample(args: argparse.Namespace) -> int:
    result = write_sample_workspace(
        args.root, data_dir=args.data_dir, truth_file=args.truth_file
    )
    print_json({"code": Code.OK, **result})
    return SUCCESS_EXIT


def cmd_import(args: argparse.Namespace) -> int:
    if args.concurrency < 1 or args.queue_limit < 1 or args.batch_timeout_ms < 0:
        print_json({"code": Code.INVALID_PARAMETER, "message": "invalid resource budget parameters"})
        return PARAMETER_EXIT

    dataset = load_dataset(args.data)
    store = Store(
        args.db,
        expected_layout_version=dataset.layout_version,
        expected_level_enum_version=dataset.level_enum_version,
    )
    try:
        report = import_dataset(
            store,
            dataset,
            concurrency=args.concurrency,
            queue_limit=args.queue_limit,
            batch_timeout_ms=args.batch_timeout_ms,
        )
        checkpoint = write_checkpoint(store, args.checkpoint)
        report["checkpoint"] = {
            "path": checkpoint["path"],
            "ledger_digest": checkpoint["payload"]["ledger_digest"],
            "previous_snapshot_version": checkpoint["previous_snapshot_version"],
            "snapshot_version_changed": checkpoint["snapshot_version_changed"],
        }
        if args.report:
            atomic_write_json(args.report, report)
        print_json(report)
        return rejection_exit_code(report)
    finally:
        store.close()


def cmd_query(args: argparse.Namespace) -> int:
    if not _db_exists(args.db):
        result = _missing_db_result(args.db)
        print_json(result)
        return PARTIAL_EXIT
    store = Store(args.db)
    try:
        if args.as_of is not None:
            result = store.query_as_of(args.as_of, args.object_key)
        else:
            result = store.query_ledger(args.object_key)
        if args.format == "table" and args.object_key is None and "objects" in result:
            print("object_key\tlevel\tstatus\tdigest\treentry")
            for item in result["objects"]:
                print(
                    f"{item['object_key']}\t{item['effective_level'] or '-'}\t"
                    f"{item['decision_status']}\t{item['basis_digest'][:12]}\t"
                    f"{int(item['reentry_flag'])}"
                )
            return SUCCESS_EXIT
        print_json(result)
        if result.get("found") is False:
            return PARTIAL_EXIT
        return SUCCESS_EXIT
    finally:
        store.close()


def cmd_export_reentry(args: argparse.Namespace) -> int:
    if not _db_exists(args.db):
        print_json(_missing_db_result(args.db))
        return PARTIAL_EXIT
    store = Store(args.db)
    try:
        report = store.export_reentry_report()
        atomic_write_json(args.out, report)
        print_json(report)
        return SUCCESS_EXIT
    finally:
        store.close()


def cmd_verify_checkpoint(args: argparse.Namespace) -> int:
    if not _db_exists(args.db):
        result = _missing_db_result(args.db)
        print_json(result)
        return PARTIAL_EXIT
    store = Store(args.db)
    try:
        result = verify_checkpoint(store, args.snapshot)
        print_json(result)
        return SUCCESS_EXIT if result["ok"] else PARTIAL_EXIT
    finally:
        store.close()


def cmd_verify_truth(args: argparse.Namespace) -> int:
    if not _db_exists(args.db):
        print_json(_missing_db_result(args.db))
        return PARTIAL_EXIT
    truth = load_truth(args.truth)
    store = Store(args.db)
    try:
        ledger_query = store.query_ledger()
        reentry_report = store.export_reentry_report()
        clock = int(truth.get("evaluation_clock", store.meta_get("evaluation_clock", "0")))
        synthesis = store.query_as_of(clock)
        result = verify_truth(
            truth,
            ledger_query=ledger_query,
            reentry_report=reentry_report,
            synthesis=synthesis,
        )
        if truth.get("expected_rejection_codes") and args.data:
            dataset = load_dataset(args.data)
            issues = [item.to_dict() for item in dataset.issues + dataset.input_failures]
            rejection_result = verify_rejection_codes(truth, issues)
            result["rejection_verification"] = rejection_result
            result["ok"] = result["ok"] and rejection_result["ok"]
        if args.out:
            atomic_write_json(args.out, result)
        print_json(result)
        return SUCCESS_EXIT if result["ok"] else PARTIAL_EXIT
    finally:
        store.close()


def cmd_recover(args: argparse.Namespace) -> int:
    from .checkpointing import recover_to_checkpoint

    if not _db_exists(args.db):
        print_json(_missing_db_result(args.db))
        return PARTIAL_EXIT
    dataset = load_dataset(args.data)
    store = Store(
        args.db,
        expected_layout_version=dataset.layout_version,
        expected_level_enum_version=dataset.level_enum_version,
    )
    try:
        result = recover_to_checkpoint(store, args.snapshot, dataset)
        atomic_write_json(args.out, result)
        print_json(result)
        return SUCCESS_EXIT if result.get("ok") else PARTIAL_EXIT
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else SUCCESS_EXIT
    except (ValueError, argparse.ArgumentError) as exc:
        print_json({"code": Code.INVALID_PARAMETER, "message": str(exc)})
        return PARAMETER_EXIT
    except FileNotFoundError as exc:
        print_json({"code": Code.DB_NOT_FOUND, "message": str(exc)})
        return PARTIAL_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
