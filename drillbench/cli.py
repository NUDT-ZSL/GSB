from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import DrillEngine
from .errors import BusinessError
from .report import build_status, inject_report, render_html, render_text
from .store import StateStore
from .util import stable_json
from .validation import load_snapshot


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=".drill/state.db",
        help="SQLite state database path",
    )


def _sample_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    if getattr(args, "sample_dir", None):
        base = Path(args.sample_dir)
        return (
            base / "plans.json",
            base / "services.json",
            base / "clock.json",
            base / "injections.json",
        )
    return (
        Path(args.plans),
        Path(args.services),
        Path(args.clock),
        Path(args.injections),
    )


def _cmd_load(args: argparse.Namespace) -> dict[str, Any]:
    plans, services, clock, injections = _sample_paths(args)
    snapshot = load_snapshot(
        plans,
        services,
        clock,
        injections,
        plan_name=args.plan,
    )
    store = StateStore(args.db)
    result = store.install_snapshot(
        snapshot,
        requested_version=args.requested_version,
    )
    return {"ok": True, **result}


def _run_args(parser: argparse.ArgumentParser, *, resume: bool) -> None:
    _add_db(parser)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan", default=None)
    parser.add_argument(
        "--skip-step",
        action="append",
        type=int,
        default=[],
        help="skip a skippable pending step before advancing",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim and execute at most one branch step",
    )
    parser.add_argument(
        "--crash-at-step",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--crash-phase",
        choices=["after-claim", "after-response"],
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(handler=_cmd_resume if resume else _cmd_run, resume=resume)


def _cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    store = StateStore(args.db)
    engine = DrillEngine(store, run_id=args.run_id)
    return engine.run(
        plan_name=args.plan,
        resume=False,
        skip_steps=tuple(args.skip_step),
        once=args.once,
        crash_at_step=args.crash_at_step,
        crash_phase=args.crash_phase,
    )


def _cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    store = StateStore(args.db)
    engine = DrillEngine(store, run_id=args.run_id)
    return engine.run(
        plan_name=args.plan,
        resume=True,
        skip_steps=tuple(args.skip_step),
        once=args.once,
        crash_at_step=args.crash_at_step,
        crash_phase=args.crash_phase,
    )


def _cmd_inject(args: argparse.Namespace) -> dict[str, Any]:
    store = StateStore(args.db)
    return inject_report(store, args.run_id)


def _cmd_status(args: argparse.Namespace) -> dict[str, Any] | str:
    store = StateStore(args.db)
    status = build_status(store, args.run_id)
    if args.format == "json":
        return {"ok": True, **status}
    if args.format == "text":
        rendered = render_text(status)
        if args.output:
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered, encoding="utf-8")
            return {
                "ok": True,
                "format": "text",
                "written": True,
                "run_id": args.run_id,
            }
        return rendered
    rendered = render_html(status)
    if not args.output:
        return rendered
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return {
        "ok": True,
        "format": "html",
        "written": True,
        "run_id": args.run_id,
    }


def _sample_manifest_check(samples_dir: Path) -> dict[str, Any]:
    manifest_path = samples_dir / "SHA256SUMS"
    issues: list[dict[str, Any]] = []
    checked: list[str] = []
    if not manifest_path.exists():
        return {
            "name": "sample_manifest",
            "ok": False,
            "detail": {"reason": "SHA256SUMS is missing"},
        }
    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            issues.append({"line": raw_line, "reason": "malformed"})
            continue
        expected, relative = parts
        relative = relative.lstrip("*")
        target = samples_dir / relative
        checked.append(relative)
        if not target.exists():
            issues.append({"file": relative, "reason": "missing"})
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            issues.append(
                {
                    "file": relative,
                    "reason": "digest_mismatch",
                    "expected": expected,
                    "actual": actual,
                }
            )
    return {
        "name": "sample_manifest",
        "ok": not issues,
        "detail": {"checked": checked, "issues": issues},
    }


def _cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    store = StateStore(args.db)
    result = store.verify_run(args.run_id)
    if args.samples_dir:
        result["checks"].append(
            _sample_manifest_check(Path(args.samples_dir))
        )
        result["ok"] = all(item["ok"] for item in result["checks"])
    return {"ok": result["ok"], **result}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drillbench",
        description="Deterministic emergency drill and fault-injection workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and install a snapshot")
    _add_db(load_parser)
    load_parser.add_argument("--sample-dir", default=None)
    load_parser.add_argument("--plans", default=None)
    load_parser.add_argument("--services", default=None)
    load_parser.add_argument("--clock", default=None)
    load_parser.add_argument("--injections", default=None)
    load_parser.add_argument("--plan", default=None)
    load_parser.add_argument("--requested-version", type=int, default=None)
    load_parser.set_defaults(handler=_cmd_load)

    run_parser = subparsers.add_parser("run", help="advance a drill run")
    _run_args(run_parser, resume=False)

    resume_parser = subparsers.add_parser(
        "resume",
        help="recover a half-committed run and replay incomplete steps",
    )
    _run_args(resume_parser, resume=True)

    inject_parser = subparsers.add_parser(
        "inject",
        help="show injection schedule, consumption state and watermark",
    )
    _add_db(inject_parser)
    inject_parser.add_argument("--run-id", required=True)
    inject_parser.set_defaults(handler=_cmd_inject)

    status_parser = subparsers.add_parser(
        "status",
        help="render stable JSON, text or standalone HTML diagnostics",
    )
    _add_db(status_parser)
    status_parser.add_argument("--run-id", required=True)
    status_parser.add_argument(
        "--format",
        choices=["json", "text", "html"],
        default="json",
    )
    status_parser.add_argument("--output", default=None)
    status_parser.set_defaults(handler=_cmd_status)

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify state, observation digest, commit chain and samples",
    )
    _add_db(verify_parser)
    verify_parser.add_argument("--run-id", required=True)
    verify_parser.add_argument("--samples-dir", default=None)
    verify_parser.set_defaults(handler=_cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            if not args.sample_dir and not all(
                [args.plans, args.services, args.clock, args.injections]
            ):
                raise BusinessError(
                    "E_BAD_ARGUMENT",
                    "provide --sample-dir or all four JSON file paths",
                )
        result = args.handler(args)
        if isinstance(result, str):
            sys.stdout.write(result)
        else:
            payload = result
            if "ok" not in payload:
                payload = {"ok": True, **payload}
            sys.stdout.write(stable_json(payload) + "\n")
        return 0 if (isinstance(result, str) or result.get("ok", True)) else 1
    except BusinessError as exc:
        sys.stdout.write(stable_json(exc.as_dict()) + "\n")
        return exc.exit_code
    except Exception as exc:
        if os.environ.get("DRILLBENCH_DEBUG") == "1":
            raise
        error = BusinessError(
            "E_INTERNAL",
            "unexpected internal failure",
            {"exception_type": type(exc).__name__, "message": str(exc)},
        )
        sys.stdout.write(stable_json(error.as_dict()) + "\n")
        return 2

