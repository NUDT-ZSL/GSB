from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from . import __version__
from .checkpoint import (
    leave_partial_snapshot,
    verify_checkpoint,
    write_checkpoint_document,
    write_snapshot,
)
from .codes import (
    EXIT_LOCK,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_USAGE,
    E_LEDGER,
)
from .generator import DEFAULT_SEED, generate_tree
from .graph import render_dot, render_mermaid
from .ledger import Ledger, LedgerPrecondition
from .locking import LockConflict, WorkbenchLock
from .parser import ParseOptions, parse_bytes
from .revision import compute_invalidation
from .util import (
    atomic_write_json,
    atomic_write_text,
    safe_file_id,
    stable_json_dumps,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bsl",
        description="BSL Binary Segment Log offline parser and diagnostic workbench",
    )
    parser.add_argument("--version", action="version", version=f"bsl-workbench {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_parser = subparsers.add_parser(
        "parse", aliases=["p"], help="parse one or more BSL files"
    )
    parse_parser.add_argument("paths", nargs="+", help="BSL files or directories")
    parse_parser.add_argument("--workdir", default=".bsl-workbench")
    parse_parser.add_argument("--file-id", help="logical file id (single input only)")
    parse_parser.add_argument("--report", help="write stable batch JSON report here")
    parse_parser.add_argument("--max-depth", type=int, default=4)
    parse_parser.add_argument("--max-expanded-bytes", type=int, default=16 * 1024 * 1024)
    parse_parser.add_argument("--hmac-key", help="HMAC key; defaults to local workbench key")
    parse_parser.add_argument("--no-resume", action="store_true")
    parse_parser.add_argument("--quiet", action="store_true")
    parse_parser.add_argument("--kill-after-files", type=int, help=argparse.SUPPRESS)
    parse_parser.add_argument(
        "--kill-point",
        choices=("after_snapshot", "before_snapshot", "before_commit"),
        default="after_snapshot",
        help=argparse.SUPPRESS,
    )
    parse_parser.set_defaults(handler=cmd_parse)

    query_parser = subparsers.add_parser(
        "query", aliases=["query-segment", "query-segments", "q"], help="query ledger segment states"
    )
    query_parser.add_argument("--workdir", default=".bsl-workbench")
    query_parser.add_argument("--file-id")
    query_parser.add_argument("--segment", "--canonical-key", dest="canonical_key")
    query_parser.add_argument("--format", choices=("json", "table"), default="json")
    query_parser.add_argument("--output")
    query_parser.set_defaults(handler=cmd_query)

    report_parser = subparsers.add_parser(
        "export-report", aliases=["report", "export"], help="export JSON diagnostic report"
    )
    report_parser.add_argument("--workdir", default=".bsl-workbench")
    report_parser.add_argument("--file-id")
    report_parser.add_argument("--output", default="diagnostic-report.json")
    report_parser.set_defaults(handler=cmd_export_report)

    checkpoint_parser = subparsers.add_parser(
        "verify-checkpoint",
        aliases=["checkpoint", "verify"],
        help="verify SQLite and snapshot checkpoint consistency",
    )
    checkpoint_parser.add_argument("--workdir", default=".bsl-workbench")
    checkpoint_parser.add_argument("--recover", action="store_true")
    checkpoint_parser.set_defaults(handler=cmd_verify_checkpoint)

    revise_parser = subparsers.add_parser(
        "revise", aliases=["revision"], help="compute revision invalidation closure"
    )
    revise_parser.add_argument("--old", required=True)
    revise_parser.add_argument("--new", required=True)
    revise_parser.add_argument("--file-id")
    revise_parser.add_argument("--output")
    revise_parser.set_defaults(handler=cmd_revise)

    graph_parser = subparsers.add_parser(
        "graph", aliases=["dependency-graph"], help="render DOT or Mermaid dependency graph"
    )
    graph_parser.add_argument("--report", required=True)
    graph_parser.add_argument("--file-id")
    graph_parser.add_argument("--format", choices=("dot", "mermaid"), default="dot")
    graph_parser.add_argument("--output")
    graph_parser.set_defaults(handler=cmd_graph)

    samples_parser = subparsers.add_parser(
        "generate-samples", aliases=["samples", "generate"], help="generate deterministic samples"
    )
    samples_parser.add_argument("--output", default="samples/generated")
    samples_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    samples_parser.set_defaults(handler=cmd_generate_samples)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except LockConflict as exc:
        envelope = {
            "ok": False,
            "code": "E_LOCK",
            "message": str(exc),
            "lock_path": exc.path,
        }
        print(stable_json_dumps(envelope), file=sys.stderr)
        return EXIT_LOCK
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        envelope = {"ok": False, "code": "E_IO", "message": str(exc)}
        print(stable_json_dumps(envelope), file=sys.stderr)
        return EXIT_PARTIAL


def _options(args: argparse.Namespace) -> ParseOptions:
    key = args.hmac_key.encode("utf-8") if getattr(args, "hmac_key", None) else None
    if key is not None:
        return ParseOptions(
            max_depth=args.max_depth,
            max_expanded_bytes=args.max_expanded_bytes,
            hmac_key=key,
        )
    return ParseOptions(
        max_depth=args.max_depth,
        max_expanded_bytes=args.max_expanded_bytes,
    )


def _expand_paths(path_values: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    for value in path_values:
        path = Path(value)
        if path.is_dir():
            paths.extend(sorted(path.rglob("*.bsl")))
        else:
            paths.append(path)
    return sorted({path.resolve(): path for path in paths}.values(), key=lambda item: str(item))


def cmd_parse(args: argparse.Namespace) -> int:
    paths = _expand_paths(args.paths)
    if not paths:
        print(stable_json_dumps({"ok": False, "code": "E_ARG", "message": "no BSL inputs found"}))
        return EXIT_USAGE
    if args.file_id and len(paths) != 1:
        print(
            stable_json_dumps(
                {"ok": False, "code": "E_ARG", "message": "--file-id requires exactly one input"}
            )
        )
        return EXIT_USAGE

    options = _options(args)
    workdir = Path(args.workdir)
    reports: list[dict[str, Any]] = []

    with WorkbenchLock(workdir):
        ledger = Ledger(workdir / "ledger.sqlite3")
        try:
            recovery = verify_checkpoint(workdir, ledger=ledger, recover=True)
            if not recovery["ok"]:
                print(stable_json_dumps(recovery))
                return EXIT_PARTIAL

            checkpoint = ledger.latest_checkpoint()
            completed: dict[str, dict[str, Any]] = (
                dict(checkpoint["completed_files"]) if checkpoint else {}
            )
            committed_in_process = 0

            for path in paths:
                file_id = args.file_id or safe_file_id(path)
                data = path.read_bytes()
                source_sha = _sha256_bytes(data)
                cached = completed.get(file_id)

                if (
                    not args.no_resume
                    and cached
                    and cached.get("source_sha256") == source_sha
                ):
                    cached_report = ledger.get_report(file_id, cached.get("generation"))
                    if cached_report is not None:
                        cached_report["from_checkpoint"] = True
                        reports.append(cached_report)
                        continue

                report = parse_bytes(
                    data,
                    file_id=file_id,
                    path=str(path),
                    options=options,
                )

                if report.get("preflight_failed"):
                    report["ledger_committed"] = False
                    reports.append(report)
                    continue

                committed_in_process += 1
                kill_before_commit = (
                    args.kill_after_files == committed_in_process
                    and args.kill_point == "before_commit"
                )
                try:
                    new_checkpoint = ledger.commit_file_report(
                        report,
                        source_path=str(path),
                        completed_files=completed,
                        kill_before_commit=kill_before_commit,
                    )
                except LedgerPrecondition:
                    report["ledger_committed"] = False
                    reports.append(report)
                    continue

                completed = dict(new_checkpoint["completed_files"])
                report["ledger_committed"] = True

                should_kill = args.kill_after_files == committed_in_process
                checkpoint_doc = write_checkpoint_document(new_checkpoint, workdir)
                if should_kill and args.kill_point == "before_snapshot":
                    leave_partial_snapshot(new_checkpoint, workdir)
                    os._exit(137)

                snapshot = write_snapshot(new_checkpoint, workdir)
                report["checkpoint_sequence"] = new_checkpoint["sequence"]
                report["checkpoint_document"] = str(checkpoint_doc)
                report["snapshot_path"] = str(snapshot)

                if should_kill and args.kill_point == "after_snapshot":
                    os._exit(137)

                reports.append(report)
        finally:
            ledger.close()

    reports.sort(key=lambda item: (item["file"]["file_id"], item["file"].get("path") or ""))
    error_files = [report for report in reports if not report["ok"]]
    all_codes = sorted(
        {
            code
            for report in reports
            for code in report.get("summary", {}).get("error_codes", [])
        }
    )
    batch = {
        "schema_version": "bsl-batch-report-v1",
        "ok": not error_files,
        "workdir": str(workdir),
        "summary": {
            "total": len(reports),
            "succeeded": len(reports) - len(error_files),
            "failed": len(error_files),
            "error_codes": all_codes,
        },
        "files": reports,
    }
    output = stable_json_dumps(batch)
    if args.report:
        atomic_write_json(args.report, batch)
    if not args.quiet:
        print(output)
    elif args.report:
        print(json.dumps({"report": args.report, "ok": batch["ok"]}, ensure_ascii=False))
    return EXIT_OK if batch["ok"] else EXIT_PARTIAL


def cmd_query(args: argparse.Namespace) -> int:
    db_path = Path(args.workdir) / "ledger.sqlite3"
    if not db_path.exists():
        envelope = {
            "ok": False,
            "code": E_LEDGER,
            "message": f"ledger does not exist: {db_path}",
        }
        print(stable_json_dumps(envelope))
        return EXIT_PARTIAL

    with Ledger(db_path) as ledger:
        rows = ledger.query_segments(args.file_id, args.canonical_key)

    result = {
        "ok": bool(rows),
        "file_id": args.file_id,
        "canonical_key": args.canonical_key,
        "count": len(rows),
        "segments": rows,
    }
    text = stable_json_dumps(result)
    if args.output:
        atomic_write_json(args.output, result)
    if args.format == "table":
        _print_segment_table(rows)
    else:
        print(text)
    return EXIT_OK if rows else EXIT_PARTIAL


def _print_segment_table(rows: list[dict[str, Any]]) -> None:
    columns = ("file_id", "canonical_key", "generation", "state_generation", "state", "watermark", "digest_status")
    print("\t".join(columns))
    for row in rows:
        print("\t".join(str(row.get(column, "")) for column in columns))


def cmd_export_report(args: argparse.Namespace) -> int:
    db_path = Path(args.workdir) / "ledger.sqlite3"
    if not db_path.exists():
        print(
            stable_json_dumps(
                {"ok": False, "code": E_LEDGER, "message": f"ledger does not exist: {db_path}"}
            )
        )
        return EXIT_PARTIAL

    with Ledger(db_path) as ledger:
        if args.file_id:
            report = ledger.get_report(args.file_id)
            if report is None:
                print(
                    stable_json_dumps(
                        {"ok": False, "code": E_LEDGER, "message": "file report not found"}
                    )
                )
                return EXIT_PARTIAL
            payload: dict[str, Any] = report
        else:
            reports = []
            for file_id in ledger.list_file_ids():
                report = ledger.get_report(file_id)
                if report is not None:
                    reports.append(report)
            reports.sort(key=lambda item: item["file"]["file_id"])
            payload = {
                "schema_version": "bsl-export-v1",
                "ok": bool(reports) and all(report.get("ok") for report in reports),
                "reports": reports,
            }

    atomic_write_json(args.output, payload)
    print(stable_json_dumps({"ok": True, "output": args.output}))
    return EXIT_OK


def cmd_verify_checkpoint(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    if args.recover:
        with WorkbenchLock(workdir):
            result = verify_checkpoint(workdir, recover=True)
    else:
        result = verify_checkpoint(workdir, recover=False)
    print(stable_json_dumps(result))
    return EXIT_OK if result["ok"] else EXIT_PARTIAL


def cmd_revise(args: argparse.Namespace) -> int:
    old_payload = json.loads(Path(args.old).read_text(encoding="utf-8"))
    new_payload = json.loads(Path(args.new).read_text(encoding="utf-8"))
    old_report = _select_report(old_payload, args.file_id)
    new_report = _select_report(new_payload, args.file_id)
    if new_report is None:
        print(stable_json_dumps({"ok": False, "code": "E_ARG", "message": "new report has no matching file"}))
        return EXIT_USAGE
    result = compute_invalidation(old_report, new_report, file_id=args.file_id)
    if args.output:
        atomic_write_json(args.output, result)
        print(stable_json_dumps({"ok": True, "output": args.output}))
    else:
        print(stable_json_dumps(result))
    return EXIT_OK


def cmd_generate_samples(args: argparse.Namespace) -> int:
    manifest = generate_tree(Path(args.output), seed=args.seed)
    print(
        stable_json_dumps(
            {
                "ok": True,
                "generated": len(manifest["files"]),
                "output": args.output,
                "seed": args.seed,
                "manifest": str(Path(args.output) / "manifest.json"),
            }
        )
    )
    return EXIT_OK


def cmd_graph(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
    if args.format == "dot":
        text = render_dot(payload, args.file_id)
    else:
        text = render_mermaid(payload, args.file_id)
    if args.output:
        target = Path(args.output)
        atomic_write_text(target, text)
        print(stable_json_dumps({"ok": True, "output": str(target), "format": args.format}))
    else:
        print(text, end="")
    return EXIT_OK


def _select_report(payload: dict[str, Any], file_id: Optional[str]) -> Optional[dict[str, Any]]:
    if payload.get("schema_version") == "bsl-batch-report-v1":
        files = payload.get("files", [])
        if file_id:
            for report in files:
                if report.get("file", {}).get("file_id") == file_id:
                    return report
            return None
        return files[0] if len(files) == 1 else None
    return payload


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
