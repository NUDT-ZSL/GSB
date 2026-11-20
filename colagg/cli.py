from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import Engine
from .errors import (
    E_IO,
    E_NOT_FOUND,
    EngineError,
)
from .recovery import recover_orphans
from .status import status_snapshot
from .storage import Store
from .validation import (
    discover_batch_files,
    load_batches,
    load_config,
    load_manifest,
    load_query,
    validate_plan_inputs,
)
from .verify import verify_workspace


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="colagg",
        description="Deterministic local columnar aggregation engine",
    )
    parser.add_argument("--version", action="version", version="colagg 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--root", default=".", help="workspace root (default: .)")
        command.add_argument(
            "--workspace",
            default="workspace/colagg.sqlite3",
            help="SQLite database path relative to --root",
        )

    load = sub.add_parser("load", help="validate and load a plan and batches")
    common(load)
    load.add_argument("--query", required=True)
    load.add_argument("--config", required=True)
    load.add_argument("--manifest", default=None)
    load.add_argument("--batches-dir", default=None)
    load.add_argument("--batch", action="append", default=[])
    load.add_argument("--mode", choices=["float", "fixed"], default=None)

    execute = sub.add_parser("execute", help="execute pending batches")
    common(execute)
    execute.add_argument("--batch", type=int, default=None)
    execute.add_argument("--once", action="store_true", help="process at most one batch")
    execute.add_argument(
        "--stop-on-error",
        action="store_true",
        help="stop after a batch-level failure",
    )

    resume = sub.add_parser("resume", help="clean crash artifacts and resume")
    common(resume)
    resume.add_argument("--once", action="store_true")

    status = sub.add_parser("status", help="print stable committed status")
    common(status)

    verify = sub.add_parser("verify", help="verify persisted state and recompute truth")
    common(verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "load":
            return _cmd_load(args)
        if args.command == "execute":
            return _cmd_execute(args)
        if args.command == "resume":
            return _cmd_resume(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "verify":
            return _cmd_verify(args)
        raise EngineError(E_NOT_FOUND, f"unknown command {args.command}")
    except EngineError as exc:
        _emit({"ok": False, "error": exc.as_dict()})
        return 2
    except OSError as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": E_IO,
                    "message": str(exc),
                    "details": {},
                },
            }
        )
        return 2


def _cmd_load(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    query = load_query(_resolve(root, args.query))
    config = load_config(_resolve(root, args.config), args.mode)
    manifest = load_manifest(_resolve(root, args.manifest) if args.manifest else None)
    paths: list[Path] = []
    if args.batches_dir:
        paths.extend(discover_batch_files(_resolve(root, args.batches_dir)))
    paths.extend(_resolve(root, item) for item in args.batch)
    if not paths:
        raise EngineError(E_IO, "provide --batches-dir or at least one --batch")
    batches = load_batches(paths, config)
    plan = validate_plan_inputs(query, config, manifest, batches)
    store = Store(_resolve(root, args.workspace), root)
    with store:
        store.initialize()
        result = store.register_plan(plan, batches)
        snapshot = status_snapshot(store, plan)
    _emit(
        {
            "ok": True,
            "command": "load",
            "mode": plan.config.numeric_mode,
            "plan_digest": plan.plan_digest,
            "batch_count": result["batch_count"],
            "pending": snapshot["progress"]["pending"],
            "state_digest": snapshot["state_digest"],
        }
    )
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    root, store, plan = _open_store(args)
    try:
        engine = Engine(store, plan)
        result = engine.execute(
            target_seq=args.batch,
            max_batches=1 if args.once else None,
            continue_on_error=not args.stop_on_error,
        )
        snapshot = status_snapshot(store, plan)
    finally:
        store.close()
    _emit(
        {
            "ok": snapshot["progress"]["failed"] == 0
            and not snapshot["failures"],
            "command": "execute",
            **result,
            "progress": snapshot["progress"],
            "failures": snapshot["failures"],
            "spill": snapshot["spill"]["totals"],
        }
    )
    return 0 if snapshot["progress"]["failed"] == 0 and not snapshot["failures"] else 3


def _cmd_resume(args: argparse.Namespace) -> int:
    root, store, plan = _open_store(args)
    try:
        recovery = recover_orphans(store, plan)
        engine = Engine(store, plan)
        execution = engine.execute(
            max_batches=1 if args.once else None,
            continue_on_error=True,
        )
        snapshot = status_snapshot(store, plan)
    finally:
        store.close()
    _emit(
        {
            "ok": snapshot["progress"]["failed"] == 0
            and not snapshot["failures"],
            "command": "resume",
            "recovery": recovery,
            "execution": execution,
            "progress": snapshot["progress"],
            "state_digest": snapshot["state_digest"],
            "failures": snapshot["failures"],
            "spill": snapshot["spill"]["totals"],
        }
    )
    return 0 if snapshot["progress"]["failed"] == 0 and not snapshot["failures"] else 3


def _cmd_status(args: argparse.Namespace) -> int:
    root, store, plan = _open_store(args)
    try:
        snapshot = status_snapshot(store, plan)
    finally:
        store.close()
    _emit({"ok": True, "command": "status", **snapshot})
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    root, store, plan = _open_store(args)
    try:
        result = verify_workspace(store, plan)
    finally:
        store.close()
    _emit({"command": "verify", **result})
    return 0 if result["ok"] else 4


def _open_store(args: argparse.Namespace) -> tuple[Path, Store, Any]:
    root = Path(args.root).resolve()
    db_path = _resolve(root, args.workspace)
    if not db_path.exists():
        raise EngineError(E_NOT_FOUND, "workspace database does not exist", path=str(args.workspace))
    store = Store(db_path, root)
    store.open()
    try:
        plan = store.load_plan()
    except BaseException:
        store.close()
        raise
    return root, store, plan


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
