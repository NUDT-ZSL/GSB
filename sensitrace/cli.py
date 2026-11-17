"""Diagnostic workbench command line interface.

Exit codes (stable contract for offline automation):

* ``0`` - command succeeded with no business diagnostics / failures;
* ``1`` - completed but some documents failed or rule violations were found;
* ``2`` - invalid command-line arguments (argparse default).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from .atomic import LockUnavailable
from .errors import sort_key
from .report import build_graph, build_report, export_report
from .samplegen import SEED, generate_samples
from .session import Session

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2


def _print_json(payload: object) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sensitrace",
        description="Offline sensitive-information flow tracking workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="import snapshot documents")
    import_parser.add_argument("--workdir", required=True)
    import_parser.add_argument("documents", nargs="+")
    import_parser.add_argument("--crash-after-tmp", action="store_true",
                               help=argparse.SUPPRESS)

    track_parser = subparsers.add_parser("track", help="print current tracking state")
    track_parser.add_argument("--workdir", required=True)

    edit_parser = subparsers.add_parser("edit-validate",
                                        help="validate an edited snapshot")
    edit_parser.add_argument("--workdir", required=True)
    edit_parser.add_argument("--document", required=True)

    export_parser = subparsers.add_parser("export", help="export report and graph")
    export_parser.add_argument("--workdir", required=True)
    export_parser.add_argument("--outdir", required=True)

    sample_parser = subparsers.add_parser("gen-samples",
                                          help="generate deterministic samples")
    sample_parser.add_argument("--outdir", required=True)
    return parser


def _cmd_import(args: argparse.Namespace) -> int:
    session = Session(args.workdir)
    try:
        outcome = session.import_documents(
            args.documents, crash_after_tmp=args.crash_after_tmp
        )
    except LockUnavailable as exc:
        _print_json({"diagnostics": [exc.diagnostic().to_dict()]})
        return EXIT_PARTIAL
    payload = {
        "applied_versions": outcome.applied_versions,
        "resumed_from_checkpoint": outcome.resumed_from_checkpoint,
        "rebuilt": outcome.rebuilt,
        "failed_sources": outcome.failed_sources,
        "diagnostic_count": len(outcome.diagnostics),
        "diagnostics": [item.to_dict() for item in sorted(outcome.diagnostics, key=sort_key)],
    }
    _print_json(payload)
    return EXIT_PARTIAL if (outcome.failed_sources or outcome.diagnostics) else EXIT_OK


def _cmd_track(args: argparse.Namespace) -> int:
    engine = Session(args.workdir).load_engine()
    if engine is None:
        _print_json({"error": "no checkpoint available; run import first"})
        return EXIT_PARTIAL
    state = engine.to_dict()
    _print_json(state)
    return EXIT_PARTIAL if engine.diagnostics else EXIT_OK


def _cmd_edit_validate(args: argparse.Namespace) -> int:
    session = Session(args.workdir)
    try:
        engine, diagnostics = session.validate_edit(args.document)
    except FileNotFoundError as exc:
        _print_json({"error": str(exc)})
        return EXIT_PARTIAL
    report = build_report(engine)
    graph = build_graph(engine)
    _print_json({
        "report": report,
        "graph_summary": {
            "node_count": len(graph["nodes"]),
            "edge_count": len(graph["edges"]),
        },
    })
    return EXIT_PARTIAL if diagnostics else EXIT_OK


def _cmd_export(args: argparse.Namespace) -> int:
    engine = Session(args.workdir).load_engine()
    if engine is None:
        _print_json({"error": "no checkpoint available; run import first"})
        return EXIT_PARTIAL
    paths = export_report(engine, args.outdir)
    _print_json({
        "exported": paths,
        "diagnostic_count": len(engine.diagnostics),
    })
    return EXIT_PARTIAL if engine.diagnostics else EXIT_OK


def _cmd_gen_samples(args: argparse.Namespace) -> int:
    written = generate_samples(args.outdir)
    _print_json({"seed": SEED, "written": written})
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    dispatch = {
        "import": _cmd_import,
        "track": _cmd_track,
        "edit-validate": _cmd_edit_validate,
        "export": _cmd_export,
        "gen-samples": _cmd_gen_samples,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
