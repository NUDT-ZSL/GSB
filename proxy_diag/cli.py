"""Command line interface: load, merge, replay, patch, resume, status, verify."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

from .errors import WorkbenchError
from .jsonio import read_json
from .paths import Paths
from .samplegen import generate as generate_samples
from .server import create_app
from .workflow import (
    load_workflow,
    merge_workflow,
    patch_workflow,
    replay_workflow,
    resume_workflow,
    status_workflow,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _paths(root: str | None) -> Paths:
    return Paths.open(root)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root",
        default=argparse.SUPPRESS,
        help="workbench root (defaults to $PROXY_DIAG_ROOT or cwd)",
    )

    parser = argparse.ArgumentParser(prog="proxy_diag", description="Cross-version proxy log diagnostics workbench")
    parser.add_argument("--root", default=None, help="workbench root")
    sub = parser.add_subparsers(dest="command", required=True)

    p_generate = sub.add_parser("generate", parents=[common], help="generate deterministic samples")
    p_generate.set_defaults(handler=cmd_generate)

    p_load = sub.add_parser("load", parents=[common], help="validate and load a log package")
    p_load.add_argument("--package", default=None, help="package descriptor JSON")
    p_load.set_defaults(handler=cmd_load)

    p_merge = sub.add_parser("merge", parents=[common], help="merge v1/v2 logs into unified view")
    p_merge.add_argument("--package", default=None)
    p_merge.set_defaults(handler=cmd_merge)

    p_replay = sub.add_parser("replay", parents=[common], help="replay forwarding decisions over loopback HTTP")
    p_replay.add_argument("--session", default=None, help="replay one connection id")
    p_replay.add_argument("--all", action="store_true", help="replay all pending attempts")
    p_replay.add_argument("--base-url", default=None, help="existing loopback service URL; starts embedded service if omitted")
    p_replay.add_argument("--plan", default=None)
    p_replay.add_argument("--worker-id", default="cli-worker")
    p_replay.set_defaults(handler=cmd_replay)

    p_patch = sub.add_parser("patch", parents=[common], help="apply an incremental table patch")
    p_patch.add_argument("patch_file_pos", nargs="?", metavar="PATCH_FILE")
    p_patch.add_argument("--file", dest="patch_file", default=None)
    p_patch.set_defaults(handler=cmd_patch)

    p_resume = sub.add_parser("resume", parents=[common], help="recover stale claims and replay uncommitted sessions")
    p_resume.add_argument("--base-url", default=None)
    p_resume.add_argument("--plan", default=None)
    p_resume.add_argument("--worker-id", default="resume-worker")
    p_resume.set_defaults(handler=cmd_resume)

    p_status = sub.add_parser("status", parents=[common], help="stable sorted JSON status")
    p_status.add_argument("--no-write", action="store_true")
    p_status.set_defaults(handler=cmd_status)

    p_serve = sub.add_parser("serve", parents=[common], help="run deterministic loopback Flask service")
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.add_argument("--plan", default=None)
    p_serve.set_defaults(handler=cmd_serve)

    p_verify = sub.add_parser("verify", parents=[common], help="run repeatable end-to-end consistency verification")
    p_verify.add_argument("--sandbox", default=None, help="verification workspace path")
    p_verify.set_defaults(handler=cmd_verify)
    return parser


def cmd_generate(args: argparse.Namespace) -> dict[str, Any]:
    paths = generate_samples(args.root)
    return {"ok": True, "command": "generate", "samples_dir": str(paths.samples)}


def cmd_load(args: argparse.Namespace) -> dict[str, Any]:
    return load_workflow(_paths(args.root), args.package)


def cmd_merge(args: argparse.Namespace) -> dict[str, Any]:
    return merge_workflow(_paths(args.root), args.package)


def cmd_replay(args: argparse.Namespace) -> dict[str, Any]:
    paths = _paths(args.root)
    if not args.session and not args.all:
        args.all = True
    return replay_workflow(
        paths,
        session_id=args.session,
        base_url=args.base_url,
        plan_path=args.plan,
        worker_id=args.worker_id,
    )


def cmd_patch(args: argparse.Namespace) -> dict[str, Any]:
    patch_file = args.patch_file or args.patch_file_pos
    if not patch_file:
        raise WorkbenchError("E_SCHEMA", "patch requires PATCH_FILE or --file")
    return patch_workflow(_paths(args.root), patch_file)


def cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    return resume_workflow(_paths(args.root), base_url=args.base_url, plan_path=args.plan, worker_id=args.worker_id)


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    return status_workflow(_paths(args.root), write=not args.no_write)


def cmd_serve(args: argparse.Namespace) -> dict[str, Any]:
    paths = _paths(args.root)
    plan_path = Path(args.plan) if args.plan else paths.samples / "test_service_plan.json"
    plan = read_json(plan_path)
    app = create_app(plan)
    server = __import__("werkzeug.serving", fromlist=["make_server"]).make_server(
        "127.0.0.1", args.port, app, threaded=True
    )
    base_url = f"http://127.0.0.1:{server.server_port}"
    print(json.dumps({"event": "listening", "base_url": base_url, "host": "127.0.0.1", "port": server.server_port}, sort_keys=True))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return {"ok": True}


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    from .verifier import run_verification

    return run_verification(_paths(args.root), sandbox=args.sandbox)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.root is None and getattr(args, "root", None) is None:
        args.root = None
    try:
        result = args.handler(args)
        if args.command == "serve":
            return int(result.get("exit_code", 0))
        _print_json(result)
        return 0 if result.get("ok", True) else 3
    except WorkbenchError as exc:
        _print_json(exc.to_dict())
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns machine-readable failure
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
