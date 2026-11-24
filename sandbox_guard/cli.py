"""Command-line diagnostic workbench."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Sequence

from .atomicio import atomic_write_json
from .codes import (
    E_ARG,
    E_INTERNAL,
    E_LOCK,
    E_PARTIAL_FAILURE,
    EXIT_BUSINESS,
    EXIT_OK,
    EXIT_USAGE,
    OK,
)
from .errors import GuardError
from .parsing import parse_patch_batch, parse_policy
from .samplegen import generate_sample
from .workspace import Workspace


def _emit(report: dict[str, Any], report_path: str | None) -> int:
    exit_code = int(report.get("exit_code", EXIT_OK if report.get("ok") else EXIT_BUSINESS))
    report["exit_code"] = exit_code
    if report_path:
        atomic_write_json(report_path, report)
        summary = report.get("summary")
        print(f"report={report_path} code={report.get('code')} exit={exit_code}")
        if summary:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def _business_codes(items: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for item in items:
        code = str(item.get("code", OK))
        counts[code] = counts.get(code, 0) + 1
    return sorted(counts)


def _read_policy(path: str | Path) -> dict[str, Any]:
    payload, error = Workspace._read_json_file(path)
    if error is not None:
        raise GuardError(E_ARG, error["message"], details=error)
    policy = parse_policy(payload)
    if not isinstance(payload, dict):
        raise GuardError(
            E_ARG,
            "policy document must be a JSON object",
            details={"rejections": policy["rejections"]},
        )
    return policy


def _planned_add_keys(paths: Sequence[str | Path]) -> set[str]:
    keys: set[str] = set()
    for path in paths:
        payload, error = Workspace._read_json_file(path)
        if error is not None:
            continue
        parsed = parse_patch_batch(payload, default_batch_id=Path(path).stem)
        if not parsed.get("ok"):
            continue
        keys.update(
            op["canonical_key"]
            for op in parsed.get("operations", [])
            if op.get("op_kind") == "add_grant"
        )
    return keys


def cmd_generate_sample(args: argparse.Namespace) -> dict[str, Any]:
    files = generate_sample(args.out, seed=args.seed)
    return {
        "ok": True,
        "code": OK,
        "command": "generate-sample",
        "files": files,
        "summary": {"seed": args.seed, "out": args.out},
        "exit_code": EXIT_OK,
    }


def cmd_import(args: argparse.Namespace) -> dict[str, Any]:
    patch_paths = list(args.patches or [])
    policy_rejections: list[dict[str, Any]] = []

    if args.rebuild:
        if not args.policy:
            raise GuardError(E_ARG, "--rebuild requires --policy POLICY.json")
        policy = _read_policy(args.policy)
        policy_rejections = policy["rejections"]
        rebuilt = Workspace.rebuild_fresh(
            args.work_dir,
            policy,
            patch_paths,
            lock_timeout_ms=args.lock_timeout_ms,
        )
        items = rebuilt["imports"]
        init = rebuilt["initialize"]
    else:
        workspace = Workspace(args.work_dir, lock_timeout_ms=args.lock_timeout_ms)
        recovery = None
        if workspace.is_initialized():
            recovery = workspace.recover(timeout_ms=args.lock_timeout_ms)

        if args.policy:
            policy = _read_policy(args.policy)
            policy_rejections = policy["rejections"]
            init = workspace.initialize_policy(policy)
        elif not workspace.is_initialized():
            raise GuardError(
                E_ARG,
                "first import requires --policy path/to/policy.json",
            )
        else:
            init = {"status": "already_initialized"}

        items = []
        planned_keys = _planned_add_keys(patch_paths)
        for patch_path in patch_paths:
            try:
                items.append(
                    workspace.import_batch_file(
                        patch_path,
                        timeout_ms=args.lock_timeout_ms,
                        extra_resolvable_grant_keys=planned_keys,
                    )
                )
            except GuardError as exc:
                items.append(
                    {
                        "status": "error",
                        "path": str(patch_path),
                        "rejections": [exc.to_dict()],
                        "applied_operations": [],
                    }
                )

    all_rejections = list(policy_rejections)
    for item in items:
        all_rejections.extend(item.get("rejections", []))

    applied = sum(1 for item in items if item.get("status") == "applied")
    duplicate = sum(1 for item in items if item.get("status") == "duplicate")
    rejected = sum(
        1
        for item in items
        if item.get("status") in {"rejected", "error"} or item.get("rejections")
    )
    ok = not all_rejections and rejected == 0
    report: dict[str, Any] = {
        "ok": ok,
        "code": OK if ok else E_PARTIAL_FAILURE,
        "command": "import-patches",
        "workspace": str(args.work_dir),
        "initialize": init if "init" in locals() else None,
        "items": items,
        "rejections": all_rejections,
        "business_codes": _business_codes(all_rejections),
        "summary": {
            "patch_files": len(patch_paths),
            "applied_batches": applied,
            "duplicate_batches": duplicate,
            "rejected_batches": rejected,
            "policy_rejections": len(policy_rejections),
            "total_rejections": len(all_rejections),
        },
        "exit_code": EXIT_OK if ok else EXIT_BUSINESS,
    }
    return report


def _workspace_with_recovery(args: argparse.Namespace) -> tuple[Workspace, dict[str, Any]]:
    workspace = Workspace(args.work_dir, lock_timeout_ms=args.lock_timeout_ms)
    recovery = workspace.recover(timeout_ms=args.lock_timeout_ms)
    return workspace, recovery


def cmd_query(args: argparse.Namespace) -> dict[str, Any]:
    selector = args.selector
    if args.pid is not None:
        selector = str(args.pid)
    elif args.profile:
        selector = args.profile
    if not selector:
        raise GuardError(
            E_ARG,
            "query-process requires a pid, profile id/key, or positional selector",
        )
    workspace, recovery = _workspace_with_recovery(args)
    result = workspace.query_process(selector, at_ms=args.at_ms)
    result["command"] = "query-process"
    result["workspace"] = str(args.work_dir)
    result["recovery"] = {
        "status": recovery.get("status"),
        "checkpoint_id": recovery.get("checkpoint_id"),
    }
    result["exit_code"] = EXIT_OK if result.get("ok") else EXIT_BUSINESS
    if result.get("ok"):
        result["summary"] = {
            "matched_processes": len(result.get("processes", [])),
            "evaluation_clock_ms": result.get("evaluation_clock_ms"),
        }
    return result


def cmd_closure(args: argparse.Namespace) -> dict[str, Any]:
    workspace, recovery = _workspace_with_recovery(args)
    result = workspace.closure_report(
        explicit_keys=args.key or None,
        include_all_events=args.all_events,
        at_ms=args.at_ms,
    )
    result["command"] = "export-closure"
    result["workspace"] = str(args.work_dir)
    result["recovery"] = {
        "status": recovery.get("status"),
        "checkpoint_id": recovery.get("checkpoint_id"),
    }
    result["exit_code"] = EXIT_OK if result.get("ok") else EXIT_BUSINESS
    if result.get("ok"):
        result["summary"] = {
            "triggers": len(result.get("trigger_keys") or []),
            "closure_grants": len(result.get("closure_grant_keys") or []),
            "affected_processes": len(result.get("affected_processes") or []),
        }
    return result


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Workspace(args.work_dir, lock_timeout_ms=args.lock_timeout_ms)
    if args.repair:
        result = workspace.recover(timeout_ms=args.lock_timeout_ms)
    else:
        result = workspace.verify_checkpoint()
    result["command"] = "verify-checkpoint"
    result["workspace"] = str(args.work_dir)
    result["exit_code"] = EXIT_OK if result.get("ok") else EXIT_BUSINESS
    return result


def cmd_shell(args: argparse.Namespace) -> dict[str, Any]:
    print("sandbox-guard interactive workbench")
    print("commands: import-patches, query-process, export-closure, verify-checkpoint, help, exit")
    while True:
        try:
            line = input("sandbox-guard> ").strip()
        except EOFError:
            print()
            break
        if not line:
            continue
        if line in {"exit", "quit"}:
            break
        if line == "help":
            print(__doc__ or "")
            print("Examples:")
            print("  import-patches --policy samples/generated/policy.json samples/generated/patches/patch-100-revoke-root.json")
            print("  query-process 4102")
            print("  export-closure")
            print("  verify-checkpoint --repair")
            continue
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as exc:
            print(json.dumps({"ok": False, "code": E_ARG, "message": str(exc)}))
            continue
        if tokens and tokens[0] in {
            "import-patches",
            "query-process",
            "export-closure",
            "verify-checkpoint",
        } and "--work-dir" not in tokens:
            tokens.extend(["--work-dir", str(args.work_dir)])
        code = main(tokens)
        print(f"[exit={code}]")
    return {"ok": True, "code": OK, "command": "shell", "exit_code": EXIT_OK}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--work-dir", default=".sandbox-guard", help="SQLite/snapshot directory")
    common.add_argument("--report", help="write stable JSON report to this path")
    common.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=0,
        help="how long to wait for the workspace file lock; 0 fails immediately with E_LOCK",
    )

    parser = argparse.ArgumentParser(
        prog="python -m sandbox_guard",
        description="Deterministic process isolation and capability diagnostic workbench",
    )
    parser.add_argument("--work-dir", default=".sandbox-guard", help="SQLite/snapshot directory")
    parser.add_argument("--report", help="write stable JSON report to this path")
    parser.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=0,
        help="how long to wait for the workspace file lock; 0 fails immediately with E_LOCK",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser(
        "generate-sample", parents=[common], help="generate fixed-seed local samples"
    )
    sample.add_argument("--seed", type=int, default=361)
    sample.add_argument("--out", default="samples/generated")
    sample.set_defaults(handler=cmd_generate_sample)

    imported = subparsers.add_parser(
        "import-patches", parents=[common], help="initialize policy and import patch batches"
    )
    imported.add_argument("patches", nargs="*", help="patch batch JSON files")
    imported.add_argument("--policy", help="policy JSON for first initialization or verification")
    imported.add_argument("--rebuild", action="store_true", help="overall rebuild instead of incremental continuation")
    imported.set_defaults(handler=cmd_import)

    query = subparsers.add_parser(
        "query-process", parents=[common], help="query deterministic effective capabilities"
    )
    query.add_argument("selector", nargs="?", help="pid, profile id/key, or process key")
    query.add_argument("--pid", type=int)
    query.add_argument("--profile")
    query.add_argument("--at-ms", type=int)
    query.set_defaults(handler=cmd_query)

    closure = subparsers.add_parser(
        "export-closure", parents=[common], help="export revocation closure and affected processes"
    )
    closure.add_argument("--key", action="append", help="grant key; may be repeated")
    closure.add_argument("--all-events", action="store_true", help="include superseded tombstone/reduction events")
    closure.add_argument("--at-ms", type=int)
    closure.set_defaults(handler=cmd_closure)

    verify = subparsers.add_parser(
        "verify-checkpoint", parents=[common], help="verify SQLite and atomic snapshot checkpoints"
    )
    verify.add_argument("--repair", action="store_true", help="restore snapshot from last complete SQLite checkpoint")
    verify.set_defaults(handler=cmd_verify)

    shell = subparsers.add_parser("shell", parents=[common], help="start interactive diagnostic REPL")
    shell.set_defaults(handler=cmd_shell)
    return parser


def _prescan_report(argv: Sequence[str]) -> str | None:
    for index, token in enumerate(argv):
        if token == "--report" and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith("--report="):
            return token.split("=", 1)[1]
    return None


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    report_path = _prescan_report(effective_argv)
    parser = build_parser()
    try:
        args = parser.parse_args(effective_argv)
        if not hasattr(args, "handler"):
            parser.error("a subcommand is required")
        # Global options may appear before or after the subcommand.  argparse
        # defaults on the main parser do not automatically inherit values from
        # subcommand positions, so parse known top-level values from raw argv.
        if "--work-dir" in effective_argv:
            idx = effective_argv.index("--work-dir")
            if idx + 1 < len(effective_argv):
                args.work_dir = effective_argv[idx + 1]
        if "--lock-timeout-ms" in effective_argv:
            idx = effective_argv.index("--lock-timeout-ms")
            if idx + 1 < len(effective_argv):
                try:
                    args.lock_timeout_ms = int(effective_argv[idx + 1])
                except ValueError:
                    pass
        if "--report" in effective_argv:
            idx = effective_argv.index("--report")
            if idx + 1 < len(effective_argv):
                args.report = effective_argv[idx + 1]
        elif any(token.startswith("--report=") for token in effective_argv):
            token = next(token for token in effective_argv if token.startswith("--report="))
            args.report = token.split("=", 1)[1]
    except SystemExit as exc:
        if int(exc.code or 0) == 0:
            return EXIT_OK
        report = {
            "ok": False,
            "code": E_ARG,
            "message": "command-line argument error",
            "exit_code": EXIT_USAGE,
        }
        if report_path:
            atomic_write_json(report_path, report)
        else:
            print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
        return EXIT_USAGE

    try:
        report = args.handler(args)
    except GuardError as exc:
        exit_code = EXIT_USAGE if exc.code == E_ARG else EXIT_BUSINESS
        report = {
            "ok": False,
            "code": exc.code,
            "command": getattr(args, "command", None),
            **exc.to_dict(),
            "exit_code": exit_code,
        }
    except (OSError, json.JSONDecodeError) as exc:
        report = {
            "ok": False,
            "code": E_PARTIAL_FAILURE,
            "message": f"I/O failure: {exc}",
            "exit_code": EXIT_BUSINESS,
        }
    except Exception as exc:  # noqa: BLE001 - CLI boundary must emit JSON.
        report = {
            "ok": False,
            "code": E_INTERNAL,
            "message": f"{type(exc).__name__}: {exc}",
            "exit_code": EXIT_BUSINESS,
        }
    return _emit(report, getattr(args, "report", None))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
