"""Interactive command-line interface."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .atomic import atomic_write_json
from .checkpoint import (
    load_checkpoint,
    verify_against_state,
    write_checkpoint,
)
from .codes import (
    E_BAD_SCHEMA,
    E_CHECKPOINT_MISMATCH,
    E_IO,
    E_NO_SNAPSHOT,
    E_OK,
    E_SNAPSHOT_MISMATCH,
    SCHEMA_VERSION,
)
from .ledger import Ledger
from .mapping import reconcile_mapping
from .models import AuditError
from .parser import (
    apply_mapping_patch,
    parse_mapping_declaration,
    parse_sample,
)
from .report import build_audit_report, build_import_report

DEFAULT_DB = "state/ledger.sqlite3"
DEFAULT_CHECKPOINT = "state/checkpoint.json"


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _error_payload(exc: AuditError) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": exc.to_dict(),
    }


def _mapping_for(bundle: Any) -> Any:
    return reconcile_mapping(
        bundle.rules,
        bundle.mapping_entries,
        declaration_generation=bundle.mapping_generation,
        conflict_policy=bundle.conflict_policy,
        direction=bundle.mapping_direction,
    )


def _write_report(path: str | Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def _require_existing_ledger(path: str | Path) -> None:
    if not Path(path).exists():
        raise AuditError(
            E_NO_SNAPSHOT,
            "ledger does not exist; run import-snapshot first",
            source_category="ledger",
            detail={"path": str(Path(path).resolve())},
        )


def command_import(args: argparse.Namespace) -> int:
    # Parse completely before opening/creating the ledger so a failed
    # precondition cannot alter existing state.
    if not args.sample and not args.mapping_patch:
        raise AuditError(
            E_BAD_SCHEMA,
            "import-snapshot requires --sample and/or --mapping-patch",
            source_category="cli",
        )
    patch = (
        parse_mapping_declaration(args.mapping_patch)
        if args.mapping_patch
        else None
    )
    bundle = parse_sample(args.sample) if args.sample else None
    if bundle is not None and patch is not None:
        bundle = apply_mapping_patch(bundle, patch)
    if bundle is None and not Path(args.db).exists():
        raise AuditError(
            E_NO_SNAPSHOT,
            "mapping patch import requires an existing ledger snapshot",
            source_category="ledger",
        )
    with Ledger(args.db) as ledger:
        if bundle is None:
            bundle = ledger.require_bundle()
            assert patch is not None
            bundle = apply_mapping_patch(bundle, patch)
        mapping = _mapping_for(bundle)
        result = ledger.import_bundle(bundle)
        report = build_import_report(
            bundle,
            mapping,
            import_result=result,
            sequence=ledger.change_seq(),
        )
        _write_report(args.report, report)
        write_checkpoint(
            args.checkpoint,
            ledger.dump_state(),
            seq=ledger.change_seq(),
        )
        summary = {
            "ok": True,
            "command": "import-snapshot",
            "ledger_sequence": ledger.change_seq(),
            "report": str(Path(args.report).resolve()),
            "checkpoint": str(Path(args.checkpoint).resolve()),
            "snapshot_replacement": result["replacement"],
            "pending_cases": result["pending_cases"],
            "mapping_codes": [
                issue.code for issue in mapping.issues
            ],
            "rejection_count": len(bundle.rejections),
        }
        _emit(summary)
    return 0


def command_adjudicate(args: argparse.Namespace) -> int:
    if args.jobs < 1:
        raise AuditError(
            E_IO,
            "--jobs must be at least 1",
            source_category="cli",
        )
    incoming = parse_sample(args.sample) if args.sample else None
    _require_existing_ledger(args.db)
    with Ledger(args.db) as ledger:
        if incoming is not None:
            stored = ledger.require_bundle()
            if (
                incoming.rule_set_fingerprint()
                != stored.rule_set_fingerprint()
                or incoming.mapping_fingerprint()
                != stored.mapping_fingerprint()
            ):
                raise AuditError(
                    E_SNAPSHOT_MISMATCH,
                    "sample does not match the imported ledger snapshot",
                    source_category="cli",
                )
        outcome = ledger.adjudicate(jobs=args.jobs, full=args.full)
        report = build_audit_report(
            outcome["bundle"],
            outcome["mapping"],
            outcome["results"],
            closure=outcome["closure"],
            mode=outcome["mode"],
            recomputed_cases=outcome["recomputed_cases"],
            sequence=outcome["sequence"],
        )
        _write_report(args.report, report)
        write_checkpoint(
            args.checkpoint,
            ledger.dump_state(),
            seq=ledger.change_seq(),
        )
        _emit(
            {
                "ok": True,
                "command": "adjudicate",
                "overall_equivalent": report["overall"]["equivalent"],
                "ledger_sequence": ledger.change_seq(),
                "report": str(Path(args.report).resolve()),
                "checkpoint": str(Path(args.checkpoint).resolve()),
                "mode": outcome["mode"],
                "recomputed_cases": outcome["recomputed_cases"],
                "summary": report["summary"],
                "business_codes": report["business_codes"],
            }
        )
    return 0


def command_export(args: argparse.Namespace) -> int:
    _require_existing_ledger(args.db)
    with Ledger(args.db) as ledger:
        exported = ledger.export_audit()
        report = build_audit_report(
            exported["bundle"],
            exported["mapping"],
            exported["results"],
            closure=exported["closure"],
            mode=exported["mode"],
            recomputed_cases=exported["recomputed_cases"],
            sequence=exported["sequence"],
            report_type="diagnostic",
        )
        _write_report(args.report, report)
        _emit(
            {
                "ok": True,
                "command": "export-report",
                "report": str(Path(args.report).resolve()),
                "ledger_sequence": exported["sequence"],
                "summary": report["summary"],
                "business_codes": report["business_codes"],
            }
        )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    checkpoint = load_checkpoint(args.checkpoint)
    checkpoint_state = checkpoint["state"]
    if Path(args.db).exists() or args.restore:
        with Ledger(args.db) as ledger:
            before = verify_against_state(
                checkpoint,
                ledger.dump_state(),
                ledger.change_seq(),
            )
            restored = False
            after = before
            if args.restore and not before["matches_ledger"]:
                ledger.replace_state(checkpoint_state)
                restored = True
                after = verify_against_state(
                    checkpoint,
                    ledger.dump_state(),
                    ledger.change_seq(),
                )
    else:
        before = {
            "valid": True,
            "matches_ledger": False,
            "code": E_CHECKPOINT_MISMATCH,
            "checkpoint_seq": checkpoint["seq"],
            "ledger_seq": None,
            "checkpoint_state_fingerprint": checkpoint["state_fingerprint"],
            "ledger_state_fingerprint": None,
        }
        restored = False
        after = before
    report = {
        "schema_version": SCHEMA_VERSION,
        "report_type": "checkpoint-verification",
        "checkpoint": str(Path(args.checkpoint).resolve()),
        "valid": True,
        "restored": restored,
        "before": before,
        "after": after,
        "code": (
            E_OK
            if after["matches_ledger"]
            else E_CHECKPOINT_MISMATCH
        ),
    }
    _write_report(args.report, report)
    _emit(report)
    if args.require_match and not after["matches_ledger"]:
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="firewall-audit",
        description=(
            "Deterministic firewall policy equivalence auditor for v1/v2 snapshots"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-snapshot",
        help="import a complete sample or apply a mapping declaration patch",
    )
    import_parser.add_argument(
        "--sample",
        help="complete sample manifest; omit only with --mapping-patch",
    )
    import_parser.add_argument(
        "--mapping-patch",
        help=(
            "mapping declaration delta; merged into the sample or current "
            "ledger snapshot"
        ),
    )
    import_parser.add_argument("--db", default=DEFAULT_DB)
    import_parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    import_parser.add_argument("--report", default="reports/import.json")
    import_parser.set_defaults(handler=command_import)

    adjudicate_parser = subparsers.add_parser(
        "adjudicate",
        help="adjudicate pending cases and emit an equivalence report",
    )
    adjudicate_parser.add_argument("--db", default=DEFAULT_DB)
    adjudicate_parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    adjudicate_parser.add_argument(
        "--report", default="reports/equivalence.json"
    )
    adjudicate_parser.add_argument("--jobs", type=int, default=1)
    adjudicate_parser.add_argument(
        "--full",
        action="store_true",
        help="recompute every case instead of only pending affected cases",
    )
    adjudicate_parser.add_argument(
        "--sample",
        help="optional sample manifest used to verify the imported fingerprint",
    )
    adjudicate_parser.set_defaults(handler=command_adjudicate)

    export_parser = subparsers.add_parser(
        "export-report",
        help="export a diagnostic report from the current SQLite ledger",
    )
    export_parser.add_argument("--db", default=DEFAULT_DB)
    export_parser.add_argument("--report", default="reports/diagnostic.json")
    export_parser.set_defaults(handler=command_export)

    verify_parser = subparsers.add_parser(
        "verify-checkpoint",
        help="validate a checkpoint and optionally restore the ledger from it",
    )
    verify_parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    verify_parser.add_argument("--db", default=DEFAULT_DB)
    verify_parser.add_argument("--report", default="reports/checkpoint.json")
    verify_parser.add_argument("--restore", action="store_true")
    verify_parser.add_argument("--require-match", action="store_true")
    verify_parser.set_defaults(handler=command_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except AuditError as exc:
        _emit(_error_payload(exc))
        return 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        error = AuditError(
            E_IO,
            str(exc),
            source_category="cli",
        )
        _emit(_error_payload(error))
        return 2


if __name__ == "__main__":
    sys.exit(main())
