"""Command-line interface for the config audit component.

Subcommands
-----------
generate-samples : write deterministic JSON fixtures
audit            : run an audit and print / persist a report
restore          : whole-state restore + bit-identical replay

Fault injection never edits source code; it only swaps input files or tampers
a generated snapshot parameter:
  --inject range   : feed the out-of-bounds archive (E_RANGE)
  --inject cycle   : feed the cyclic derivation archive (E_CYCLE)
  --inject grid    : remove the grid block from a snapshot before restore (E_GRID)
  --inject config  : feed the malformed archive (E_CONFIG)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import run_audit
from .errors import AuditError
from .generate_samples import write_samples
from .logging_utils import StructuredLogger
from .snapshot import (
    current_archive_versions,
    save_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="config-audit",
                                     description="Fixed-point config audit")
    parser.add_argument("--log", help="path to structured JSON-lines log")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-samples")
    gen.add_argument("--out", required=True)

    au = sub.add_parser("audit")
    au.add_argument("--inputs", nargs="+", required=True)
    au.add_argument("--places", type=int, required=True)
    au.add_argument("--abs-tolerance", default="0")
    au.add_argument("--relative-ppm", type=int, default=0)
    au.add_argument("--report", help="write canonical JSON report here")
    au.add_argument("--snapshot", help="write session snapshot here")
    au.add_argument("--inject", choices=["range", "cycle", "config"],
                    help="inject an input fault (no source edits)")

    rs = sub.add_parser("restore")
    rs.add_argument("--inputs", nargs="+", required=True)
    rs.add_argument("--snapshot", required=True)
    rs.add_argument("--report")
    rs.add_argument("--inject", choices=["grid"],
                    help="strip grid block from snapshot (E_GRID)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logger = StructuredLogger(args.log)

    try:
        if args.command == "generate-samples":
            paths = write_samples(args.out)
            logger.log("samples_generated", count=len(paths), out=args.out)
            print(json.dumps({"written": [str(p) for p in paths]}, indent=2))
            return 0

        if args.command == "audit":
            return _cmd_audit(args, logger)

        if args.command == "restore":
            return _cmd_restore(args, logger)

    except AuditError as exc:
        logger.log_error("audit_failure", exc.code, exc.message,
                         key=exc.key, grid=exc.grid, locations=exc.locations)
        print(json.dumps(exc.to_dict(), indent=2, ensure_ascii=False),
              file=sys.stderr)
        return 2

    logger.log_error("audit_failure", "E_CONFIG", "unreachable command")
    return 3


def _cmd_audit(args, logger: StructuredLogger) -> int:
    inputs = list(args.inputs)
    if args.inject:
        sample_dir = _ensure_samples()
        inputs = [str(sample_dir / {
            "range": "node_range_bad.json",
            "cycle": "node_cycle.json",
            "config": "node_bad.json",
        }[args.inject])]
        logger.log("fault_injected", kind=args.inject, inputs=inputs,
                   grid=args.places)

    report = run_audit(
        inputs,
        places=args.places,
        abs_tolerance=args.abs_tolerance,
        relative_ppm=args.relative_ppm,
    )
    logger.log("audit_complete", grid=report["grid"]["signature"],
               total_keys=report["summary"]["total_keys"],
               total_pairs=report["summary"]["total_pairs"],
               hash=report["canonical_hash"])

    if args.report:
        _write_json(Path(args.report), report)
    if args.snapshot:
        versions = current_archive_versions(inputs)
        path = save_snapshot(report, versions, args.snapshot)
        logger.log("snapshot_saved", path=str(path),
                   grid=report["grid"]["signature"])
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def _cmd_restore(args, logger: StructuredLogger) -> int:
    from .snapshot import load_snapshot, restore_and_replay
    snapshot_path = Path(args.snapshot)
    if args.inject == "grid":
        # Parameter-level tampering only: drop the precision grid block.
        doc = json.loads(snapshot_path.read_text(encoding="utf-8"))
        grid_places = doc["grid"].get("places")
        doc.pop("grid", None)
        snapshot_path.write_text(json.dumps(doc, sort_keys=True, indent=2),
                                 encoding="utf-8")
        logger.log("fault_injected", kind="grid",
                   grid=grid_places, snapshot=str(snapshot_path))

    try:
        load_snapshot(snapshot_path)
    except AuditError as exc:
        logger.log_error("audit_failure", exc.code, exc.message,
                         key=exc.key, grid=exc.grid, locations=exc.locations)
        print(json.dumps(exc.to_dict(), indent=2, ensure_ascii=False),
              file=sys.stderr)
        return 2

    report = restore_and_replay(list(args.inputs), snapshot_path)
    logger.log("restore_complete", action=report["restore"]["action"],
               hash=report["canonical_hash"])
    if args.report:
        _write_json(Path(args.report), report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def _ensure_samples() -> Path:
    sample_dir = Path.cwd() / "runs" / "_injected_samples"
    if not any(sample_dir.glob("*.json")):
        write_samples(sample_dir)
    return sample_dir


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
