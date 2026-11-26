from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .engine import Engine
from .errors import EngineError
from .hashing import canonical_text
from .origin import load_origin_content, start_origin_server
from .seed import generate_sample
from .storage import Store


DEFAULT_STATE = "var/cdn-state.db"


def print_json(value: Any) -> None:
    print(canonical_text(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdn-engine",
        description="Deterministic CDN edge-cache consistency rule engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    seed_parser = sub.add_parser("seed", help="generate deterministic sample inputs")
    seed_parser.add_argument("--out", default="samples/demo")
    seed_parser.add_argument("--port", type=int, default=18080)
    seed_parser.add_argument("--offline-port", type=int, default=18099)

    load_parser = sub.add_parser("load", help="validate/load a sample or atomically load a rule package")
    load_parser.add_argument("path", nargs="?", help="sample directory or package directory")
    load_parser.add_argument("--state", default=DEFAULT_STATE)
    load_parser.add_argument("--sample")
    load_parser.add_argument("--package")
    load_parser.add_argument("--clock", type=int)
    load_parser.add_argument("--crash-before-commit", action="store_true", help=argparse.SUPPRESS)

    merge_parser = sub.add_parser("merge", help="merge v1 and v2 rules")
    merge_parser.add_argument("--state", default=DEFAULT_STATE)
    merge_parser.add_argument("--clock", type=int)

    probe_parser = sub.add_parser("probe", help="advance observations and adjudicate windows")
    probe_parser.add_argument("--state", default=DEFAULT_STATE)
    probe_parser.add_argument("--file", help="optional observation batch to submit idempotently")
    probe_parser.add_argument("--clock", type=int)
    probe_parser.add_argument("--node")
    probe_parser.add_argument("--rule")
    probe_parser.add_argument("--recompute-closure")
    probe_parser.add_argument("--crash-after-obs", type=int)
    probe_parser.add_argument("--slow-ms", type=int, default=0)
    probe_parser.add_argument("--no-origin", action="store_true")

    patch_parser = sub.add_parser("patch", help="submit an incremental rule patch")
    patch_parser.add_argument("--state", default=DEFAULT_STATE)
    patch_parser.add_argument("patch_path", nargs="?")
    patch_parser.add_argument("--file")
    patch_parser.add_argument("--clock", type=int)

    resume_parser = sub.add_parser("resume", help="recover committed state and finish open windows")
    resume_parser.add_argument("--state", default=DEFAULT_STATE)
    resume_parser.add_argument("--clock", type=int)

    status_parser = sub.add_parser("status", help="show durable engine status")
    status_parser.add_argument("--state", default=DEFAULT_STATE)

    list_parser = sub.add_parser("conclusions", help="list committed consistency conclusions")
    list_parser.add_argument("--state", default=DEFAULT_STATE)
    list_parser.add_argument("--node")
    list_parser.add_argument("--rule")

    verify_parser = sub.add_parser("verify", help="run repeatable end-to-end verification")
    verify_parser.add_argument("--workspace", default=".verify-run")
    verify_parser.add_argument("--port", type=int, default=18080)
    verify_parser.add_argument("--offline-port", type=int, default=18099)

    serve_parser = sub.add_parser("serve-origin", help="run deterministic local origin service")
    serve_parser.add_argument("--content", default="samples/demo/origin-content.json")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=18080)

    return parser


def _engine(state_path: str) -> tuple[Store, Engine]:
    store = Store(state_path)
    return store, Engine(store)


def run(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "seed":
        result = generate_sample(Path(args.out), port=args.port, offline_port=args.offline_port)
        print_json({"ok": True, "command": "seed", **result})
        return 0

    if args.command == "serve-origin":
        content = load_origin_content(args.content)
        server = start_origin_server(
            content,
            host=args.host,
            port=args.port,
            content_path=Path(args.content),
        )
        host, port = server.server_address[:2]
        print(json.dumps({"ok": True, "origin_base_url": f"http://{host}:{port}"}, sort_keys=True))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
        return 0

    if args.command == "verify":
        from .verification import run_verification

        result = run_verification(
            Path(args.workspace),
            requested_port=args.port,
            offline_port=args.offline_port,
        )
        print_json(result)
        return 0 if result.get("ok") else 1

    store, engine = _engine(args.state)
    try:
        if args.command == "load":
            package_path = args.package
            sample_path = args.sample
            if args.path:
                if (Path(args.path) / "package-manifest.json").exists():
                    package_path = package_path or args.path
                else:
                    sample_path = sample_path or args.path
            if package_path:
                result = engine.load_package(
                    package_path,
                    args.clock,
                    crash_before_commit=args.crash_before_commit,
                )
            else:
                result = engine.load_sample(sample_path or "samples/demo", args.clock)
        elif args.command == "merge":
            result = engine.merge(args.clock)
        elif args.command == "probe":
            submission = engine.import_observation_file(args.file) if args.file else None
            if args.recompute_closure:
                result = engine.recompute_closure(
                    args.recompute_closure,
                    args.clock,
                    node_name=args.node,
                    origin_enabled=not args.no_origin,
                )
            else:
                result = engine.probe(
                    args.clock,
                    node_name=args.node,
                    rule_name=args.rule,
                    crash_after_obs=args.crash_after_obs,
                    slow_ms=args.slow_ms,
                    origin_enabled=not args.no_origin,
                )
            if submission is not None and isinstance(result, dict):
                result["observation_submission"] = submission
        elif args.command == "patch":
            patch_file = args.file or getattr(args, "patch_path", None)
            if not patch_file:
                raise EngineError(ErrorCode.COMMAND_FAILED, "patch requires --file <patch.json> or a positional path")
            result = engine.apply_patch_file(patch_file, args.clock)
        elif args.command == "resume":
            result = engine.resume(args.clock)
        elif args.command == "status":
            result = engine.status()
        elif args.command == "conclusions":
            result = {
                "ok": True,
                "command": "conclusions",
                "conclusions": engine.list_conclusions(node_name=args.node, rule_name=args.rule),
            }
        else:  # pragma: no cover - argparse enforces commands
            raise EngineError(ErrorCode.COMMAND_FAILED, f"unknown command: {args.command}")
        print_json(result)
        return 0
    except EngineError as exc:
        print(canonical_text(exc.to_dict()), file=sys.stderr)
        return exc.exit_code
    finally:
        store.close()


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
