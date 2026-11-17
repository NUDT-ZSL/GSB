from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .engine import EngineConfig, execute
from .errors import EngineError
from .shuffle import MAX_PARTITIONS
from .verify import verify_artifacts


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_command(args)
    if args.command == "verify":
        return _verify_command(args)
    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="Fixed-point shuffle hash join engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="execute one local join experiment")
    run_parser.add_argument("--tables", default="data/sample_tables.json", help="two-table JSON file")
    run_parser.add_argument("--query", default="data/sample_query.json", help="query JSON file")
    run_parser.add_argument("--partitions", default="4", help="positive partition count up to 64")
    run_parser.add_argument("--scale-factor", default="100", help="integer power of ten used for fixed-point quantization")
    run_parser.add_argument("--output", default="artifacts/run", help="output directory")
    run_parser.add_argument("--inject", choices=["none", "nullmap", "scalef", "part"], default="none")
    run_parser.add_argument("--quiet", action="store_true")

    verify_parser = subparsers.add_parser("verify", help="independently verify experiment reports")
    verify_parser.add_argument("--tables", default="data/sample_tables.json")
    verify_parser.add_argument("--query", default="data/sample_query.json")
    verify_parser.add_argument("--reports", nargs="*", default=[], help="report.json files")
    verify_parser.add_argument("--manifest", help="demo manifest JSON")
    verify_parser.add_argument("--output", default="artifacts/verification.json")
    verify_parser.add_argument("--quiet", action="store_true")
    return parser


def _run_command(args: argparse.Namespace) -> int:
    output_dir = Path(args.output)
    events: list[dict[str, Any]] = []

    def record(event: str, **fields: Any) -> None:
        events.append({"seq": len(events) + 1, "event": event, **fields})

    try:
        _ensure_writable_directory(output_dir)
    except EngineError as error:
        _emit_console({"status": "ERROR", "error": error.to_dict()})
        return 2

    injection = None if args.inject == "none" else args.inject
    tables_path = Path(args.tables)
    query_path = Path(args.query)
    partitions = args.partitions
    scale_factor = args.scale_factor

    try:
        if injection == "nullmap":
            mutated_path = output_dir / "injected_tables.json"
            _make_nullmap_injection(tables_path, mutated_path)
            tables_path = mutated_path
            record("INPUT_INJECTED", injection="nullmap", source=str(args.tables), mutated_file=str(mutated_path))
        elif injection == "scalef":
            scale_factor = "25"
            record("PARAMETER_INJECTED", injection="scalef", scale_factor=scale_factor)
        elif injection == "part":
            partitions = str(MAX_PARTITIONS + 1)
            record("PARAMETER_INJECTED", injection="part", partitions=partitions)
        elif injection is not None:
            raise EngineError("E_IO", "unknown injection", details={"injection": injection})

        report = execute(
            EngineConfig(
                tables_path=tables_path,
                query_path=query_path,
                partitions=partitions,
                scale_factor=scale_factor,
                injection=injection,
            )
        )
    except EngineError as error:
        record("ERROR", code=error.code, message=error.message, table=error.table, row=error.row, column=error.column)
        error_report = {
            "schema_version": "1.0",
            "status": "ERROR",
            "injection": injection,
            "code": error.code,
            "parent_code": error.parent_code,
            "message": error.message,
            "table": error.table,
            "row": error.row,
            "column": error.column,
            "details": error.details,
            "events": events,
        }
        _write_json(output_dir / "error.json", error_report)
        _write_events(output_dir / "events.jsonl", events)
        if not args.quiet:
            _emit_console({"status": "ERROR", "code": error.code, "error": error.to_dict(), "output": str(output_dir)})
        return 2

    _write_json(output_dir / "report.json", report)
    _write_events(output_dir / "events.jsonl", report["events"])
    if not args.quiet:
        _emit_console(
            {
                "status": "OK",
                "output": str(output_dir),
                "rows": len(report["rows"]),
                "partitions": report["config"]["partitions"],
                "scale_factor": report["config"]["scale_factor"],
                "partition_digest": report["shuffle"]["global_digest"],
                "output_digest": report["determinism"]["output_digest"],
                "sum_fixed_lattice": report["aggregate"]["sum_fixed_lattice"],
                "mean_fixed_lattice": report["aggregate"]["mean_fixed_lattice"],
                "dominant_segment": report["error_propagation"]["dominant_segment"],
            }
        )
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    try:
        result = verify_artifacts(
            tables_path=args.tables,
            query_path=args.query,
            report_paths=list(args.reports),
            manifest_path=args.manifest,
            output_path=args.output,
        )
    except EngineError as error:
        result = {"status": "ERROR", "code": error.code, "error": error.to_dict()}
        _write_json(Path(args.output), result)
    if not args.quiet:
        _emit_console(result if result.get("status") != "OK" else {
            "status": "OK",
            "checked_reports": result["checked_reports"],
            "injection_checks": len(result["injection_checks"]),
            "output": args.output,
        })
    return 0 if result.get("status") == "OK" else 2


def _ensure_writable_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise EngineError("E_IO", f"output directory is not writable: {exc}", details={"path": str(path)}) from exc


def _make_nullmap_injection(source_path: Path, target_path: Path) -> None:
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError("E_IO", f"cannot prepare nullmap injection: {exc}", details={"path": str(source_path)}) from exc
    mutated = False
    for table in payload.get("tables", []):
        nullable_columns = [column.get("name") for column in table.get("columns", []) if column.get("nullable")]
        for column_name in nullable_columns:
            bits = table.get("null_bitmap", {}).get(column_name)
            if isinstance(bits, list) and bits:
                bits.pop()
                mutated = True
                break
        if mutated:
            break
    if not mutated:
        raise EngineError("E_IO", "nullmap injection could not find a non-empty nullable bitmap")
    target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) for event in events]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _emit_console(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
