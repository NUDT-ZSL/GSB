"""Command-line entry point with stable 0/1/2 exit-code semantics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .checkpoint import LockConflict
from .constants import EXIT_ARGUMENT_ERROR, EXIT_PARTIAL
from .diagnostics import Diagnostic
from .engine import EventDocument, Workbench
from .util import parse_json_with_offset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="authdiag",
        description=(
            "Offline deterministic authentication policy invalidation workbench"
        ),
    )
    parser.add_argument("policy", help="policy declaration JSON file")
    parser.add_argument(
        "events",
        nargs="+",
        help="one or more event stream JSON files (array or JSONL)",
    )
    parser.add_argument(
        "--out",
        default="out",
        help="diagnostic output directory (default: out)",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="checkpoint and lock directory (default: same as --out)",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="run without reading or writing a checkpoint",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    policy_path = Path(args.policy)
    output_dir = Path(args.out)
    workdir = Path(args.workdir) if args.workdir else output_dir
    initial_diagnostics: list[Diagnostic] = []
    policy_document: Any = {
        "schema_version": "1.0",
        "policy_version": "0.0.0",
        "entries": [],
        "updates": [],
        "sync_markers": [],
    }

    try:
        policy_raw = policy_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        initial_diagnostics.append(
            Diagnostic(
                code="E_IO",
                message=f"cannot read policy file: {exc}",
                source=str(policy_path),
            )
        )
    else:
        parsed, offset = parse_json_with_offset(policy_raw)
        if offset is not None:
            initial_diagnostics.append(
                Diagnostic(
                    code="E_JSON",
                    message="policy file is not valid JSON",
                    source=str(policy_path),
                    offset=offset,
                )
            )
        else:
            policy_document = parsed

    event_documents: list[EventDocument] = []
    for raw_path in args.events:
        path = Path(raw_path)
        try:
            event_raw = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            event_documents.append(
                EventDocument(
                    source=str(path),
                    load_error=Diagnostic(
                        code="E_IO",
                        message=f"cannot read event stream: {exc}",
                        source=str(path),
                    ),
                )
            )
        else:
            event_documents.append(EventDocument(source=str(path), raw=event_raw))

    workbench = Workbench(
        policy_document,
        source=str(policy_path),
        workdir=workdir,
        output_dir=output_dir,
        checkpoint_enabled=not args.no_checkpoint,
        initial_diagnostics=initial_diagnostics,
    )
    try:
        result = workbench.run(event_documents)
    except LockConflict as exc:
        _print_json(exc.diagnostic.to_dict())
        return EXIT_PARTIAL
    except (OSError, ValueError, TypeError) as exc:
        diagnostic = Diagnostic(
            code="E_IO",
            message=f"workbench failed: {exc}",
            source=str(workdir),
        )
        _print_json(diagnostic.to_dict())
        return EXIT_PARTIAL
    except Exception as exc:  # pragma: no cover - last-resort diagnostic boundary
        diagnostic = Diagnostic(
            code="E_INTERNAL",
            message=f"unexpected workbench failure: {exc}",
            source=str(workdir),
        )
        _print_json(diagnostic.to_dict())
        return EXIT_PARTIAL

    _print_json(
        {
            "status": result.report["status"],
            "exit_code": result.exit_code,
            "report": result.output_paths.get("report"),
            "dependency_graph": result.output_paths.get("dependency_graph_json"),
            "checkpoint_status": result.report["checkpoint"]["status"],
            "summary": result.report["summary"],
        }
    )
    return result.exit_code


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
