"""Interactive command line with parseable JSON reports."""

from __future__ import annotations

import argparse
import glob
import json
import sys

from . import atomic, codes
from .attribution import attribute_hosts
from .spec import BaselineSpec
from .store import DriftEngine


def _load_spec(path: str) -> BaselineSpec:
    return BaselineSpec.load_file(path)


def _collect_inputs(manifest_path: str) -> tuple[list[str], list[tuple[str, str]]]:
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    base = manifest.get("base_dir", ".")
    v1_paths = sorted(glob.glob(f"{base}/{pattern}") for pattern in manifest.get("v1_globs", []))
    v1_paths = sorted(item for group in v1_paths for item in group)
    v2_targets = []
    for target in manifest.get("v2_targets", []):
        v2_targets.append((f"{base}/{target['shard_dir']}", f"{base}/{target['sqlite']}"))
    return v1_paths, sorted(v2_targets)


def _emit(report_path: str | None, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if report_path:
        atomic.atomic_write_bytes(report_path, text.encode("utf-8"))
    print(text)


def _partial(payload: dict) -> int:
    return codes.EXIT_PARTIAL if payload.get("rejected_records") or payload.get("fatal_snapshots") else codes.EXIT_OK


def cmd_import(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    v1_paths, v2_targets = _collect_inputs(args.manifest)
    engine = DriftEngine(args.db, spec, args.checkpoint_dir)
    try:
        report = engine.import_batch(
            args.batch_id, v1_paths, v2_targets,
            args.window_seconds, args.decision_clock_ms, workers=args.workers,
        )
    finally:
        engine.close()
    _emit(args.report, report)
    return _partial(report)


def cmd_decide(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    engine = DriftEngine(args.db, spec, args.checkpoint_dir)
    try:
        result = engine.full_rebuild(args.window_seconds, args.decision_clock_ms)
        rows = result["decisions"]
        if args.host:
            rows = [row for row in rows if row["host"] == args.host]
        payload = {
            "window_seconds": args.window_seconds,
            "decision_clock_ms": args.decision_clock_ms,
            "decisions": rows,
            "conflicts": result["conflicts"],
        }
    finally:
        engine.close()
    _emit(args.report, payload)
    return codes.EXIT_OK


def cmd_attribute(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    engine = DriftEngine(args.db, spec, args.checkpoint_dir)
    try:
        result = engine.full_rebuild(args.window_seconds, args.decision_clock_ms)
        attribution = attribute_hosts(result["decisions"], spec)
        if args.host:
            attribution = [row for row in attribution if row["host"] == args.host]
        payload = {"attribution": attribution}
    finally:
        engine.close()
    _emit(args.report, payload)
    return codes.EXIT_PARTIAL if any(row["code"] == codes.E_DEP_CYCLE for row in attribution) else codes.EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    engine = DriftEngine(args.db, spec, args.checkpoint_dir)
    try:
        payload = engine.verify_checkpoint()
    finally:
        engine.close()
    _emit(args.report, payload)
    return codes.EXIT_PARTIAL if payload["code"] == codes.E_CHECKPOINT_RESIDUE else codes.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drift-engine",
                                     description="Offline drift attribution and adjudication engine")
    parser.add_argument("--spec", required=True, help="path to baseline spec.json")
    parser.add_argument("--db", required=True, help="SQLite ledger path")
    parser.add_argument("--checkpoint-dir", required=True, help="checkpoint snapshot directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="import an evidence batch and advance the ledger")
    p_import.add_argument("--manifest", required=True)
    p_import.add_argument("--batch-id", required=True)
    p_import.add_argument("--window-seconds", type=int, required=True)
    p_import.add_argument("--decision-clock-ms", type=int, required=True)
    p_import.add_argument("--workers", type=int, default=1)
    p_import.add_argument("--report")
    p_import.set_defaults(func=cmd_import)

    p_decide = sub.add_parser("decide", help="query disposition decisions")
    p_decide.add_argument("--window-seconds", type=int, required=True)
    p_decide.add_argument("--decision-clock-ms", type=int, required=True)
    p_decide.add_argument("--host")
    p_decide.add_argument("--report")
    p_decide.set_defaults(func=cmd_decide)

    p_attr = sub.add_parser("attribute", help="export drift attribution closure")
    p_attr.add_argument("--window-seconds", type=int, required=True)
    p_attr.add_argument("--decision-clock-ms", type=int, required=True)
    p_attr.add_argument("--host")
    p_attr.add_argument("--report")
    p_attr.set_defaults(func=cmd_attribute)

    p_verify = sub.add_parser("verify", help="validate the last checkpoint against the ledger")
    p_verify.add_argument("--report")
    p_verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return args.func(args)
    except FileNotFoundError as exc:
        print(json.dumps({"code": codes.E_SNAPSHOT_FATAL, "message": str(exc)}), file=sys.stderr)
        return codes.EXIT_USAGE
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"code": codes.E_SNAPSHOT_FATAL, "message": str(exc)}), file=sys.stderr)
        return codes.EXIT_USAGE
