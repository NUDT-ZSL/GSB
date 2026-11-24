"""Interactive JSON command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .checkpoint import CheckpointManager
from .engine import run_detection_batch
from .errors import DetectorError, InputError, LockUnavailable
from .io_utils import (
    WorkspaceLock,
    atomic_write_json,
    stable_json_text,
)
from .parsing import (
    compute_input_digest,
    compute_plan_digest,
    parse_sample_dir,
    plan_with_overrides,
)
from .report import build_report
from .rules import build_rule_catalog
from .workspace import Workspace


def _emit(value: Any, *, stream: Any = sys.stdout) -> None:
    print(stable_json_text(value), file=stream)


def _error_payload(code: str, message: str) -> dict:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _manager_for(
    workspace: Workspace,
    *,
    input_digest: str,
    plan_digest: str,
    catalog_fingerprint: str,
) -> CheckpointManager:
    return CheckpointManager(
        workspace.root,
        input_digest=input_digest,
        plan_digest=plan_digest,
        catalog_fingerprint=catalog_fingerprint,
    )


def _import_sample(workspace: Workspace, sample_dir: Path) -> tuple[Any, str]:
    sample = parse_sample_dir(sample_dir)
    input_digest = compute_input_digest(sample)
    workspace.save_normalized(sample, input_digest)
    return sample, input_digest


def command_import(args: argparse.Namespace) -> int:
    workspace = Workspace(Path(args.workspace))
    with WorkspaceLock(workspace.lock_path):
        sample, input_digest = _import_sample(workspace, Path(args.sample_dir))
    catalog = build_rule_catalog(sample.rules)
    payload = {
        "ok": not sample.rejections,
        "source": "import",
        "sample_dir": str(Path(args.sample_dir).resolve()),
        "workspace": str(workspace.root.resolve()),
        "input_digest": input_digest,
        "message_records": len(sample.messages),
        "connections": len({item.connection_id for item in sample.messages}),
        "rule_records": len(sample.rules),
        "rejections": [item.to_doc() for item in sample.rejections],
        "rule_catalog": catalog.to_doc(),
        "plan": sample.plan.to_doc() if sample.plan else None,
    }
    _emit(payload)
    return 0 if not sample.rejections else 1


def command_detect(args: argparse.Namespace) -> int:
    workspace = Workspace(Path(args.workspace))
    with WorkspaceLock(workspace.lock_path):
        if args.sample_dir:
            sample, input_digest = _import_sample(
                workspace, Path(args.sample_dir)
            )
        else:
            sample, input_digest = workspace.load_normalized()
        if sample.plan is None:
            raise InputError("normalized input has no plan")
        plan = plan_with_overrides(
            sample.plan,
            concurrency=args.concurrency,
            timeout_ms=args.timeout_ms,
            budget_ms=args.budget_ms,
            queue_limit=args.queue_limit,
            output_path=args.output,
        )
        catalog = build_rule_catalog(sample.rules)
        plan_digest = compute_plan_digest(plan)
        manager = _manager_for(
            workspace,
            input_digest=input_digest,
            plan_digest=plan_digest,
            catalog_fingerprint=catalog.fingerprint,
        )
        outcome = run_detection_batch(
            sample,
            plan,
            manager,
            resume=args.resume,
        )
        report = build_report(
            sample=sample,
            plan=plan,
            input_digest=input_digest,
            plan_digest=plan_digest,
            catalog=outcome["catalog"],
            connections=outcome["connections"],
            summary=outcome["summary"],
            scheduling=outcome["scheduling"],
            checkpoint=outcome["checkpoint"],
        )
        report["output_path"] = str(
            workspace.report_path(plan.output_path).resolve()
        )
        atomic_write_json(workspace.report_path(plan.output_path), report)
    _emit(report)
    return 0 if report["ok"] else 1


def _read_consistent_checkpoint_results(
    workspace: Workspace,
) -> dict[str, dict]:
    if not workspace.checkpoint_db.is_file() or not workspace.checkpoint_snapshot.is_file():
        raise InputError(
            "checkpoint pair not found; run detect before query"
        )
    manager = CheckpointManager(
        workspace.root,
        input_digest="",
        plan_digest="",
        catalog_fingerprint="",
    )
    loaded = manager.load(require_identity=False)
    if not loaded.valid:
        raise InputError(f"checkpoint is not complete: {loaded.reason}")
    return loaded.completed


def command_query(args: argparse.Namespace) -> int:
    workspace = Workspace(Path(args.workspace))
    results = _read_consistent_checkpoint_results(workspace)
    if args.connection:
        result = results.get(args.connection)
        if result is None:
            _emit(
                {
                    "ok": False,
                    "error": {
                        "code": "E_NOT_FOUND",
                        "message": f"connection not found: {args.connection}",
                    },
                }
            )
            return 1
        payload = {
            "ok": True,
            "source": "checkpoint",
            "count": 1,
            "connections": [result],
        }
    else:
        payload = {
            "ok": True,
            "source": "checkpoint",
            "count": len(results),
            "connections": [results[key] for key in sorted(results)],
        }
    _emit(payload)
    return 0


def command_verify_checkpoint(args: argparse.Namespace) -> int:
    workspace = Workspace(Path(args.workspace))
    with WorkspaceLock(workspace.lock_path):
        input_digest = ""
        plan_digest = ""
        catalog_fingerprint = ""
        if workspace.normalized_path.is_file():
            sample, input_digest = workspace.load_normalized()
            if sample.plan is None:
                raise InputError("normalized input has no plan")
            plan_digest = compute_plan_digest(sample.plan)
            catalog_fingerprint = build_rule_catalog(sample.rules).fingerprint
        manager = _manager_for(
            workspace,
            input_digest=input_digest,
            plan_digest=plan_digest,
            catalog_fingerprint=catalog_fingerprint,
        )
        payload = manager.verify()
    payload["ok"] = bool(payload.get("valid"))
    if payload.get("valid") and not payload.get("identity_matches"):
        payload["warning"] = (
            "checkpoint is internally consistent but its input/plan/rule "
            "identity differs from the currently imported sample"
        )
    _emit(payload)
    return 0 if payload["ok"] else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smuggle-detector",
        description=(
            "Offline deterministic HTTP request-smuggling detector and "
            "evidence-chain verifier"
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import", help="strictly parse and persist a sample directory"
    )
    import_parser.add_argument("--sample-dir", required=True)
    import_parser.add_argument("--workspace", default=".smuggle-state")
    import_parser.set_defaults(handler=command_import)

    detect_parser = subparsers.add_parser(
        "detect", help="run bounded parallel detection"
    )
    detect_parser.add_argument("--workspace", default=".smuggle-state")
    detect_parser.add_argument(
        "--sample-dir",
        help="parse this sample directory again before detection",
    )
    detect_parser.add_argument("--output", help="override plan output_path")
    detect_parser.add_argument("--concurrency", type=int)
    detect_parser.add_argument("--timeout-ms", type=int)
    detect_parser.add_argument("--budget-ms", type=int)
    detect_parser.add_argument("--queue-limit", type=int)
    detect_parser.add_argument(
        "--resume",
        action="store_true",
        help="resume only from a fully consistent committed checkpoint",
    )
    detect_parser.set_defaults(handler=command_detect)

    query_parser = subparsers.add_parser(
        "query", help="query committed per-connection decisions"
    )
    query_parser.add_argument("--workspace", default=".smuggle-state")
    query_parser.add_argument("--connection")
    query_parser.set_defaults(handler=command_query)

    verify_parser = subparsers.add_parser(
        "verify-checkpoint",
        help="replay-check SQLite and JSON checkpoint consistency",
    )
    verify_parser.add_argument("--workspace", default=".smuggle-state")
    verify_parser.set_defaults(handler=command_verify_checkpoint)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except LockUnavailable as exc:
        _emit(_error_payload(exc.code, exc.message), stream=sys.stderr)
        return 1
    except (InputError, DetectorError) as exc:
        _emit(_error_payload(exc.code, exc.message), stream=sys.stderr)
        return 2
    except Exception as exc:
        _emit(
            _error_payload("E_INTERNAL", f"{type(exc).__name__}: {exc}"),
            stream=sys.stderr,
        )
        return 1

