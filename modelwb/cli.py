from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from .errors import WorkbenchError
from .workbench import Workbench


def _default_db() -> str:
    return os.environ.get("MODELWB_DB", "var/workbench.db")


def _print(value: Any, *, pretty: bool = False) -> None:
    if pretty:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def parse_stages(value: str) -> list[int]:
    parts = [item for item in re.split(r"[\s,]+", value.strip()) if item]
    if not parts:
        raise argparse.ArgumentTypeError("at least one gray stage is required")
    try:
        return [int(item) for item in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid gray stage list: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modelwb",
        description="Offline two-generation model manifest and gray-release workbench",
    )
    parser.add_argument(
        "--db",
        default=_default_db(),
        help="workbench SQLite state database (default: MODELWB_DB or var/workbench.db)",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="load and validate v1/v2 catalogs")
    load.add_argument("--input-dir", help="directory containing generated samples")
    load.add_argument("--v1", dest="v1_path", help="v1 JSON manifest")
    load.add_argument("--v2-db", dest="v2_db_path", help="v2 SQLite metadata database")
    load.add_argument("--v2-dir", dest="v2_dir", help="v2 shard JSON directory")
    load.add_argument("--registries", dest="registries_path", help="optional registry JSON")
    load.add_argument("--replicas", dest="replicas_path", help="optional replicas JSON")
    load.add_argument("--baseline-version", help="initial deployed version")
    load.add_argument("--op-id", help="caller supplied idempotency key")
    load.add_argument(
        "--clock-tolerance",
        type=int,
        default=50,
        help="permitted logical clock rollback (default: 50)",
    )

    patch = subparsers.add_parser("patch", help="apply an incremental shard patch")
    patch.add_argument("--patch", required=True, help="patch JSON")
    patch.add_argument("--full", dest="full_path", help="full replacement manifest for verification")
    patch.add_argument("--op-id", help="caller supplied idempotency key")
    patch.add_argument("--clock-tolerance", type=int, default=50)

    gray = subparsers.add_parser("gray", help="prewarm and advance gray traffic stages")
    gray.add_argument("--target-version", required=True)
    gray.add_argument("--stages", required=True, type=parse_stages, help="e.g. 10,50,100")
    gray.add_argument("--run-id")
    gray.add_argument("--fail-replica", help="deterministically inject a prewarm digest failure")
    gray.add_argument("--op-id")

    rollback = subparsers.add_parser("rollback", help="reverse gray stages and restore baseline")
    rollback.add_argument("--run-id")
    rollback.add_argument("--reason", default="failure")
    rollback.add_argument("--trigger", default="operator")
    rollback.add_argument("--window-ms", type=int, default=5000)
    rollback.add_argument("--op-id")

    resume = subparsers.add_parser("resume", help="resume interrupted prepared or running operations")
    resume.add_argument("--op-id")

    status = subparsers.add_parser("status", help="show persisted workbench state")
    status.add_argument("--compact", action="store_true", help="omit large catalog view")

    verify = subparsers.add_parser("verify", help="compare incremental patch with full replacement")
    verify.add_argument("--patch", required=True)
    verify.add_argument("--full", dest="full_path")
    verify.add_argument("--base-version")
    verify.add_argument(
        "--input-dir",
        help="temporary sample directory to load if the selected state database is empty",
    )

    versions = subparsers.add_parser("versions", help="show the merged unified version view")
    request = subparsers.add_parser("request", help="bind a request to a stable snapshot")
    request.add_argument("--request-id", required=True)
    request.add_argument("--finish", action="store_true")

    operation = subparsers.add_parser("operation", help="show one operation and its durable steps")
    operation.add_argument("--op-id", required=True)
    for subparser in subparsers.choices.values():
        subparser.add_argument(
            "--db",
            default=argparse.SUPPRESS,
            help="state database; may also be supplied before the subcommand",
        )
        subparser.add_argument(
            "--pretty",
            action="store_true",
            default=argparse.SUPPRESS,
            help="pretty-print JSON; may also be supplied before the subcommand",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            workbench = Workbench(args.db)
            catalog_loaded = bool(workbench.storage.catalog().all_versions)
            if catalog_loaded:
                result = workbench.verify_patch(
                    patch_path=args.patch,
                    full_path=args.full_path,
                    base_version=args.base_version,
                )
            else:
                detected_input = args.input_dir or _detect_input_dir(args.patch, args.full_path)
                if not detected_input:
                    raise WorkbenchError(
                        "INVALID_STATE",
                        "verify requires a loaded state database or --input-dir",
                    )
                with tempfile.TemporaryDirectory(prefix="modelwb-verify-") as temporary:
                    temp_workbench = Workbench(Path(temporary) / "verify.db")
                    temp_workbench.load(input_dir=detected_input)
                    result = temp_workbench.verify_patch(
                        patch_path=args.patch,
                        full_path=args.full_path,
                        base_version=args.base_version,
                    )
            _print(result, pretty=args.pretty)
            return 0
        workbench = Workbench(args.db)
        if args.command == "load":
            result = workbench.load(
                input_dir=args.input_dir,
                v1_path=args.v1_path,
                v2_db_path=args.v2_db_path,
                v2_dir=args.v2_dir,
                registries_path=args.registries_path,
                replicas_path=args.replicas_path,
                baseline_version=args.baseline_version,
                op_id=args.op_id,
                clock_tolerance=args.clock_tolerance,
            )
        elif args.command == "patch":
            result = workbench.patch(
                patch_path=args.patch,
                full_path=args.full_path,
                op_id=args.op_id,
                clock_tolerance=args.clock_tolerance,
            )
        elif args.command == "gray":
            result = workbench.gray(
                target_version=args.target_version,
                stages=args.stages,
                run_id=args.run_id,
                fail_replica=args.fail_replica,
                op_id=args.op_id,
            )
        elif args.command == "rollback":
            result = workbench.rollback(
                run_id=args.run_id,
                reason=args.reason,
                trigger=args.trigger,
                window_ms=args.window_ms,
                op_id=args.op_id,
            )
        elif args.command == "resume":
            result = workbench.resume(args.op_id)
            if not result.get("ok", False):
                _print(result, pretty=args.pretty)
                return 2
        elif args.command == "status":
            result = workbench.status()
            if args.compact:
                result = {key: value for key, value in result.items() if key != "versions"}
        elif args.command == "versions":
            result = workbench.versions()
        elif args.command == "request":
            result = (
                workbench.finish_request(args.request_id)
                if args.finish
                else workbench.start_request(args.request_id)
            )
        elif args.command == "operation":
            result = workbench.storage.get_operation(args.op_id)
        else:
            parser.error(f"unknown command: {args.command}")
            return 2
        _print(result, pretty=args.pretty)
        return 0
    except WorkbenchError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


def _detect_input_dir(patch_path: str, full_path: str | None) -> str | None:
    candidates = [Path(patch_path).resolve().parent]
    if full_path:
        candidates.append(Path(full_path).resolve().parent)
    for path in list(candidates):
        candidates.append(path.parent)
    for candidate in candidates:
        if (candidate / "v1_manifest.json").exists() and (candidate / "v2" / "meta.db").exists():
            return str(candidate)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
