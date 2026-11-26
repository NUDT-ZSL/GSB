from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__
from .closure import assert_patch_generation_not_rollback, compute_closures
from .config import read_json, read_v2_package, validate_v1_config
from .configservice import install_config_package, install_initial_config, recover_pending_config
from .db import connect, default_db_path, get_meta, immediate, init_db, set_meta
from .engine import adjudicate_batch, plan_baseline_blocks
from .errors import BizError, ErrorCode
from .loader import load_manifest, persist_inputs, validate_patch
from .runtime import DEFAULT_CLOCK_SKEW_TOLERANCE_MS, file_lock
from .scheduler import recover_stale_tasks, run_until_settled
from .seeddata import FIXED_SEED, generate_fixed_samples
from .verify import status_report, verify_workspace


def emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def open_db(args: argparse.Namespace):
    data_dir = Path(args.data_dir)
    db_path = Path(args.db) if args.db else default_db_path(data_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    init_db(conn)
    return conn, db_path


def injected_clock(conn, args: argparse.Namespace) -> int:
    if args.clock_ms is not None:
        return int(args.clock_ms)
    return int(get_meta(conn, "clock_watermark_ms", "0") or 0)


def tolerance(args: argparse.Namespace) -> int:
    return int(getattr(args, "clock_skew_tolerance_ms", DEFAULT_CLOCK_SKEW_TOLERANCE_MS))


def command_seed(args: argparse.Namespace) -> Dict[str, Any]:
    summary = generate_fixed_samples(Path(args.data_dir), seed=args.seed)
    return {"ok": True, "command": "seed", **summary}


def command_load(args: argparse.Namespace) -> Dict[str, Any]:
    data_dir = Path(args.data_dir)
    validate_v1_config(
        read_json(
            data_dir / "config_v1.json",
            malformed_code=ErrorCode.CONFIG_V1_INVALID,
        )
    )
    read_v2_package(data_dir / "config_v2")
    conn, db_path = open_db(args)
    try:
        clock = injected_clock(conn, args)
        summary = persist_inputs(conn, data_dir, clock, tolerance(args))
        with file_lock(data_dir / "locks" / "config.lock"):
            config = install_initial_config(conn, data_dir, clock, tolerance(args))
        status = status_report(conn)
        return {
            "ok": True,
            "command": "load",
            "db_path": str(db_path),
            "loaded": summary,
            "config": config,
            "status": status["counts"],
        }
    finally:
        conn.close()


def command_generate(args: argparse.Namespace) -> Dict[str, Any]:
    conn, db_path = open_db(args)
    data_dir = Path(args.data_dir)
    try:
        clock = injected_clock(conn, args)
        with immediate(conn):
            from .db import advance_clock

            advance_clock(conn, clock, tolerance(args))
        planned = plan_baseline_blocks(conn, clock, args.case_id)
        recovery = recover_stale_tasks(conn, db_path, data_dir, clock)
        schedule = run_until_settled(
            conn,
            db_path,
            data_dir,
            clock,
            budget_ms=args.budget_ms,
            concurrency=args.concurrency,
            case_id=args.case_id,
            retry_failed=False,
            max_rounds=args.max_rounds,
            scope="generate",
        )
        return {
            "ok": True,
            "command": "generate",
            "planned_task_count": len(planned),
            "new_task_count": sum(1 for p in planned if p["created"]),
            "recovery": recovery,
            "schedule": schedule,
            "status": status_report(conn),
        }
    finally:
        conn.close()


def _read_submitted_patches(path: Path):
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise BizError(
            ErrorCode.PATCH_INVALID,
            "submitted patch file does not exist",
            {"path": str(path)},
        ) from exc
    if not text:
        return []
    try:
        if path.suffix == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            raw = json.loads(text)
            if isinstance(raw, dict) and isinstance(raw.get("patches"), list):
                rows = raw["patches"]
            elif isinstance(raw, dict) and raw.get("patch_id"):
                rows = [raw]
            elif isinstance(raw, list):
                rows = raw
            else:
                raise BizError(ErrorCode.PATCH_INVALID, "unrecognized patch file format")
        return rows
    except json.JSONDecodeError as exc:
        raise BizError(
            ErrorCode.PATCH_INVALID,
            "submitted patch file is malformed JSON",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc


def _insert_submitted_patches(conn, data_dir, path, clock, skew_tolerance):
    rows = _read_submitted_patches(path)
    tasks = load_manifest(data_dir)
    tasks_by_id = {task["case_id"]: task for task in tasks}
    inserted = []
    with immediate(conn):
        from .db import advance_clock

        advance_clock(conn, clock, skew_tolerance)
        for index, row in enumerate(rows):
            patch = validate_patch(row, tasks_by_id, index)
            existing = conn.execute(
                "SELECT patch_id FROM patches WHERE patch_id=?", (patch["patch_id"],)
            ).fetchone()
            if existing is not None:
                inserted.append({"patch_id": patch["patch_id"], "idempotent": True})
                continue
            assert_patch_generation_not_rollback(conn, patch)
            conn.execute(
                "INSERT INTO patches("
                "patch_id,case_id,block_start,block_end,block_range_json,patch_kind,"
                "patch_generation,logical_clock_ms,new_digest,content_json,refs_json,"
                "received_clock_ms,applied,superseded) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)",
                (
                    patch["patch_id"],
                    patch["case_id"],
                    patch["block_start"],
                    patch["block_end"],
                    json.dumps(patch["block_range"]),
                    patch["patch_kind"],
                    patch["patch_generation"],
                    patch["logical_clock_ms"],
                    patch["new_digest"],
                    json.dumps(patch.get("patch_content", {}), sort_keys=True),
                    json.dumps(patch.get("refs", []), sort_keys=True),
                    clock,
                ),
            )
            inserted.append({"patch_id": patch["patch_id"], "idempotent": False})
    return inserted


def command_patch(args: argparse.Namespace) -> Dict[str, Any]:
    conn, db_path = open_db(args)
    data_dir = Path(args.data_dir)
    try:
        clock = injected_clock(conn, args)
        inserted = []
        if args.file:
            inserted = _insert_submitted_patches(
                conn, data_dir, Path(args.file), clock, tolerance(args)
            )
        else:
            with immediate(conn):
                from .db import advance_clock

                advance_clock(conn, clock, tolerance(args))
        closure = compute_closures(conn, clock, args.case_id)
        recovery = recover_stale_tasks(conn, db_path, data_dir, clock)
        schedule = run_until_settled(
            conn,
            db_path,
            data_dir,
            clock,
            budget_ms=args.budget_ms,
            concurrency=args.concurrency,
            case_id=args.case_id,
            retry_failed=False,
            max_rounds=args.max_rounds,
            scope="patch_recompute",
        )
        return {
            "ok": True,
            "command": "patch",
            "inserted": inserted,
            "closure": closure,
            "recovery": recovery,
            "schedule": schedule,
            "status": status_report(conn),
        }
    finally:
        conn.close()


def command_resume(args: argparse.Namespace) -> Dict[str, Any]:
    conn, db_path = open_db(args)
    data_dir = Path(args.data_dir)
    try:
        clock = injected_clock(conn, args)
        with file_lock(data_dir / "locks" / "config.lock"):
            pending_config = recover_pending_config(conn, data_dir, clock, tolerance(args))
        with immediate(conn):
            from .db import advance_clock

            advance_clock(conn, clock, tolerance(args))
        recovery = recover_stale_tasks(conn, db_path, data_dir, clock)
        schedule = run_until_settled(
            conn,
            db_path,
            data_dir,
            clock,
            budget_ms=args.budget_ms,
            concurrency=args.concurrency,
            case_id=args.case_id,
            retry_failed=True,
            max_attempts=args.max_attempts,
            max_rounds=args.max_rounds,
            scope="resume",
        )
        return {
            "ok": True,
            "command": "resume",
            "pending_config": pending_config,
            "recovery": recovery,
            "schedule": schedule,
            "status": status_report(conn),
        }
    finally:
        conn.close()


def command_status(args: argparse.Namespace) -> Dict[str, Any]:
    conn, _db_path = open_db(args)
    try:
        return {"ok": True, "command": "status", **status_report(conn)}
    finally:
        conn.close()


def command_verify(args: argparse.Namespace) -> Dict[str, Any]:
    data_dir = Path(args.data_dir)
    db_path = Path(args.db) if args.db else default_db_path(data_dir)
    report = verify_workspace(
        data_dir,
        db_path,
        deep_recompute=not args.no_deep,
        clock_ms=args.clock_ms,
        clock_skew_tolerance_ms=tolerance(args),
    )
    report["command"] = "verify"
    if not report["ok"]:
        emit(report)
        raise SystemExit(4)
    if args.strict and not report["complete"]:
        report["ok"] = False
        report["strict_failure"] = "incomplete tasks remain"
        emit(report)
        raise SystemExit(3)
    emit(report)
    return {}


def command_reconfigure(args: argparse.Namespace) -> Dict[str, Any]:
    conn, _db_path = open_db(args)
    data_dir = Path(args.data_dir)
    try:
        clock = injected_clock(conn, args)
        with file_lock(data_dir / "locks" / "config.lock"):
            result = install_config_package(
                conn,
                Path(args.package),
                data_dir,
                clock,
                tolerance(args),
            )
        return {"ok": True, "command": "reconfigure", "config": result}
    finally:
        conn.close()


def command_adjudicate(args: argparse.Namespace) -> Dict[str, Any]:
    conn, _db_path = open_db(args)
    try:
        clock = injected_clock(conn, args)
        with immediate(conn):
            from .db import advance_clock

            clock = advance_clock(conn, clock, tolerance(args))
        config_version = args.config_version
        report = adjudicate_batch(conn, clock, config_version, args.tolerance)
        return {"ok": True, "command": "adjudicate", "batch": report}
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dataplane",
        description="Deterministic training/inference data and cache consistency workbench",
    )
    parser.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default="data", help="deterministic sample/data directory")
    common.add_argument("--db", default=None, help="SQLite state DB (default: <data-dir>/state.sqlite3)")
    common.add_argument("--clock-ms", type=int, default=None, help="injected logical clock")
    common.add_argument(
        "--clock-skew-tolerance-ms",
        type=int,
        default=DEFAULT_CLOCK_SKEW_TOLERANCE_MS,
    )
    common.add_argument("--debug", action="store_true", help="print traceback for unexpected errors")

    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", parents=[common], help="generate fixed-seed local samples")
    seed.add_argument("--seed", type=int, default=FIXED_SEED)
    seed.set_defaults(func=command_seed)

    load = sub.add_parser("load", parents=[common], help="validate and load all inputs")
    load.set_defaults(func=command_load)

    generate = sub.add_parser("generate", parents=[common], help="plan/materialize baseline caches")
    generate.add_argument("--case-id", default=None)
    generate.add_argument("--budget-ms", type=int, default=10_000)
    generate.add_argument("--concurrency", type=int, default=4)
    generate.add_argument("--max-rounds", type=int, default=1_000)
    generate.set_defaults(func=command_generate)

    patch = sub.add_parser("patch", parents=[common], help="adjudicate patches and recompute closures")
    patch.add_argument("--file", default=None, help="optional new patch JSON/JSONL file")
    patch.add_argument("--case-id", default=None)
    patch.add_argument("--budget-ms", type=int, default=10_000)
    patch.add_argument("--concurrency", type=int, default=4)
    patch.add_argument("--max-rounds", type=int, default=1_000)
    patch.set_defaults(func=command_patch)

    resume = sub.add_parser("resume", parents=[common], help="recover leases and replay missing work")
    resume.add_argument("--case-id", default=None)
    resume.add_argument("--budget-ms", type=int, default=10_000)
    resume.add_argument("--concurrency", type=int, default=4)
    resume.add_argument("--max-rounds", type=int, default=1_000)
    resume.add_argument("--max-attempts", type=int, default=2)
    resume.set_defaults(func=command_resume)

    status = sub.add_parser("status", parents=[common], help="show persisted state summary")
    status.set_defaults(func=command_status)

    verify = sub.add_parser("verify", parents=[common], help="verify hashes, ledgers and recomputation")
    verify.add_argument("--no-deep", action="store_true")
    verify.add_argument("--strict", action="store_true", help="fail if incomplete tasks remain")
    verify.set_defaults(func=command_verify)

    reconfigure = sub.add_parser("reconfigure", parents=[common], help="install a v2 config package")
    reconfigure.add_argument("--package", required=True)
    reconfigure.set_defaults(func=command_reconfigure)

    adjudicate = sub.add_parser("adjudicate", parents=[common], help="rebuild a batch adjudication ledger")
    adjudicate.add_argument("--config-version", type=int, default=None)
    adjudicate.add_argument("--tolerance", type=float, default=None)
    adjudicate.set_defaults(func=command_adjudicate)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        if result:
            emit(result)
        return 0
    except BizError as exc:
        emit(exc.to_dict())
        return exc.exit_code
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI exception boundary
        payload = {
            "ok": False,
            "error_code": ErrorCode.COMMAND_FAILED,
            "error": str(exc),
            "type": type(exc).__name__,
        }
        if getattr(args, "debug", False):
            payload["traceback"] = traceback.format_exc()
        emit(payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
