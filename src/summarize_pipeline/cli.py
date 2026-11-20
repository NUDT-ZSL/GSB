from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import load_config
from .errors import ErrorCode, PipelineError
from .generator import generate_bundle
from .pipeline import load_documents, run_all_precisions
from .snapshots import SnapshotStore, build_snapshot
from .utils import read_json, write_json_atomic
from .validator import validate_report, verify_reproducibility


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="summary-pipeline",
        description="Deterministic summarization pipeline with numeric error attribution",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="generate deterministic JSONL samples and config"
    )
    generate.add_argument("--data", default="data/samples.jsonl")
    generate.add_argument("--config", default="configs/default.json")
    generate.add_argument("--seed", type=int, default=20261121)
    generate.add_argument("--docs", type=int, default=12)

    run = subparsers.add_parser("run", help="run all three precision tiers")
    run.add_argument("--input", default="data/samples.jsonl")
    run.add_argument("--config", default="configs/default.json")
    run.add_argument("--out", default="artifacts/run.json")
    run.add_argument("--snapshot-dir", default="artifacts/snapshots")
    run.add_argument("--snapshot-version", type=int)
    run.add_argument("--no-snapshot", action="store_true")

    validate = subparsers.add_parser(
        "validate", help="validate a report and rerun twice for reproducibility"
    )
    validate.add_argument("--input", default="data/samples.jsonl")
    validate.add_argument("--config", default="configs/default.json")
    validate.add_argument("--result")
    validate.add_argument("--out", default="artifacts/validation.json")

    all_command = subparsers.add_parser(
        "all", help="generate, run, snapshot, validate, and verify reproducibility"
    )
    all_command.add_argument("--data", default="data/samples.jsonl")
    all_command.add_argument("--config", default="configs/default.json")
    all_command.add_argument("--run-out", default="artifacts/run.json")
    all_command.add_argument("--validation-out", default="artifacts/validation.json")
    all_command.add_argument("--snapshot-dir", default="artifacts/snapshots")
    all_command.add_argument("--snapshot-version", type=int)
    all_command.add_argument("--seed", type=int, default=20261121)
    all_command.add_argument("--docs", type=int, default=12)
    all_command.add_argument("--no-snapshot", action="store_true")
    return parser


def _print_summary(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))


def _generate(args: argparse.Namespace) -> int:
    rows, _ = generate_bundle(
        args.data,
        args.config,
        seed=args.seed,
        doc_count=args.docs,
    )
    _print_summary(
        {
            "ok": True,
            "command": "generate",
            "rows": len(rows),
            "data": str(Path(args.data)),
            "config": str(Path(args.config)),
        }
    )
    return 0


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    documents, input_errors = load_documents(args.input)
    report = run_all_precisions(
        documents, config, input_errors=input_errors
    )
    write_json_atomic(args.out, report, pretty=True)
    snapshot_info: dict[str, Any] | None = None
    if not args.no_snapshot:
        store = SnapshotStore(args.snapshot_dir)
        version = (
            args.snapshot_version
            if args.snapshot_version is not None
            else max(config.version, store.high_water_version + 1, 1)
        )
        snapshot = build_snapshot(report, config, version)
        stored = store.commit(snapshot)
        snapshot_info = {
            "version": stored["snapshot_version"],
            "checksum": stored["checksum"],
            "path": str(store.current_path),
        }
    _print_summary(
        {
            "ok": True,
            "command": "run",
            "run_hash": report["run_hash"],
            "processed_docs": len(documents),
            "errors": len(report["input_errors"])
            + sum(len(result["errors"]) for result in report["precisions"].values()),
            "out": str(Path(args.out)),
            "snapshot": snapshot_info,
        }
    )
    return 0


def _validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    documents, input_errors = load_documents(args.input)
    reproducibility = verify_reproducibility(
        documents, config, input_errors=input_errors
    )
    loaded_validation: dict[str, Any] | None = None
    if args.result:
        report = read_json(args.result)
        loaded_validation = validate_report(report, config)
    ok = reproducibility["ok"] and (
        loaded_validation is None or loaded_validation["ok"]
    )
    payload = {
        "ok": ok,
        "command": "validate",
        "reproducibility": reproducibility,
        "loaded_validation": loaded_validation,
    }
    write_json_atomic(args.out, payload, pretty=True)
    _print_summary(
        {
            "ok": ok,
            "command": "validate",
            "first_run_hash": reproducibility["reproducibility"]["first_run_hash"],
            "second_run_hash": reproducibility["reproducibility"]["second_run_hash"],
            "hash_match": reproducibility["reproducibility"]["hash_match"],
            "issue_count": reproducibility["issue_count"]
            + (
                loaded_validation["issue_count"]
                if loaded_validation is not None
                else 0
            ),
            "out": str(Path(args.out)),
        }
    )
    return 0 if ok else 2


def _all(args: argparse.Namespace) -> int:
    generate_bundle(
        args.data,
        args.config,
        seed=args.seed,
        doc_count=args.docs,
    )
    config = load_config(args.config)
    documents, input_errors = load_documents(args.data)
    report = run_all_precisions(documents, config, input_errors=input_errors)
    write_json_atomic(args.run_out, report, pretty=True)
    snapshot_info: dict[str, Any] | None = None
    if not args.no_snapshot:
        store = SnapshotStore(args.snapshot_dir)
        version = (
            args.snapshot_version
            if args.snapshot_version is not None
            else max(config.version, store.high_water_version + 1, 1)
        )
        snapshot = build_snapshot(report, config, version)
        stored = store.commit(snapshot)
        snapshot_info = {
            "version": stored["snapshot_version"],
            "checksum": stored["checksum"],
            "path": str(store.current_path),
        }
    validation = verify_reproducibility(
        documents, config, input_errors=input_errors
    )
    loaded_validation = validate_report(read_json(args.run_out), config)
    validation["loaded_validation"] = loaded_validation
    validation["ok"] = validation["ok"] and loaded_validation["ok"]
    write_json_atomic(args.validation_out, validation, pretty=True)
    _print_summary(
        {
            "ok": validation["ok"],
            "command": "all",
            "run_hash": report["run_hash"],
            "reproducibility_hash_match": validation["reproducibility"][
                "hash_match"
            ],
            "validation_ok": loaded_validation["ok"],
            "rows": sum(len(doc.sentences) for doc in documents),
            "processed_docs": len(documents),
            "run_out": str(Path(args.run_out)),
            "validation_out": str(Path(args.validation_out)),
            "snapshot": snapshot_info,
        }
    )
    return 0 if validation["ok"] else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            return _generate(args)
        if args.command == "run":
            return _run(args)
        if args.command == "validate":
            return _validate(args)
        if args.command == "all":
            return _all(args)
    except PipelineError as exc:
        print(
            json.dumps(
                {"ok": False, "error": exc.to_dict()},
                ensure_ascii=True,
                sort_keys=True,
                indent=2,
            )
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": ErrorCode.UNATTRIBUTED,
                        "stage": "cli",
                        "doc_id": "*",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                },
                ensure_ascii=True,
                sort_keys=True,
                indent=2,
            )
        )
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())

