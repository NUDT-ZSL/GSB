"""Command line interface for the local gateway middleware."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .errors import BusinessError
from .injection import (
    INJECTIONS,
    inject_corrupt_policy,
    inject_token_version_conflict,
    inject_zero_window,
    restore,
    validate_injected_inputs,
)
from .ledger import Ledger
from .runner import evolve_policy, recover, run_file


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def cmd_run(args: argparse.Namespace) -> int:
    result = run_file(
        args.routes,
        args.tokens,
        args.requests,
        args.workdir,
        batch_size=args.batch_size,
        replay_after=args.replay,
    )
    _print(
        {
            "code": "E_OK",
            "batches": result.batches,
            "decision_counts": result.engine.decision_codes(),
            "consistency": result.consistency,
            "selection": result.selection,
            "recovery": result.recovery,
            "snapshots": "workdir/snapshots/*.json",
        }
    )
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    _print({"code": "E_OK", **recover(args.workdir, replay=not args.no_replay)})
    return 0


def cmd_evolve(args: argparse.Namespace) -> int:
    _print({"code": "E_OK", **evolve_policy(
        args.workdir, args.routes, args.tokens, args.requests, args.batch_size
    )})
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    source_routes = Path(args.routes)
    source_tokens = Path(args.tokens)
    # Never mutate checked-in inputs: stage private copies inside the workdir.
    stage_dir = Path(args.workdir) / "injection_stage"
    stage_dir.mkdir(parents=True, exist_ok=True)
    routes_path = stage_dir / source_routes.name
    token_path = stage_dir / source_tokens.name
    shutil.copy2(source_routes, routes_path)
    shutil.copy2(source_tokens, token_path)
    backup_dir = Path(args.workdir) / "injection_backups"
    ledger = Ledger(Path(args.workdir) / "ledger.db")
    try:
        if args.kind == "corrupt_policy":
            backup = inject_corrupt_policy(routes_path, backup_dir)
        elif args.kind == "token_version_conflict":
            backup = inject_token_version_conflict(token_path, backup_dir)
        elif args.kind == "zero_window":
            backup, _ = inject_zero_window(routes_path, backup_dir)
        else:  # pragma: no cover - argparse choices guard this
            raise ValueError(args.kind)
        error = validate_injected_inputs(args.kind, routes_path, token_path, backup_dir)
        ledger.log_event(error.code, f"injection_{args.kind}", {
            "message": error.message, "file": error.file, "line": error.line,
            "source_file": str(source_routes if args.kind != "token_version_conflict"
                               else source_tokens),
        })
        _print({"code": error.code, "event": f"injection_{args.kind}",
                "message": error.message, "file": error.file, "line": error.line,
                "expected_code": INJECTIONS[args.kind].code,
                "staged_copy": str(stage_dir)})
        restore(backup, routes_path if args.kind != "token_version_conflict" else token_path)
        return 3
    finally:
        ledger.close()


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import GatewayServer

    server = GatewayServer(args.port, args.workdir, args.routes, args.tokens)
    print(json.dumps({"code": "E_OK", "listening": f"127.0.0.1:{args.port}"}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        server.ledger.close()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway", description="Local auth+ratelimit gateway")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="process an NDJSON request file")
    run.add_argument("--routes", required=True)
    run.add_argument("--tokens", required=True)
    run.add_argument("--requests", required=True)
    run.add_argument("--workdir", required=True)
    run.add_argument("--batch-size", type=int, default=20)
    run.add_argument("--replay", action="store_true")
    run.set_defaults(func=cmd_run)

    rec = sub.add_parser("recover", help="recover from a ledger after forced termination")
    rec.add_argument("--workdir", required=True)
    rec.add_argument("--no-replay", action="store_true")
    rec.set_defaults(func=cmd_recover)

    evo = sub.add_parser("evolve", help="load a new, strictly higher policy version")
    evo.add_argument("--routes", required=True)
    evo.add_argument("--tokens", required=True)
    evo.add_argument("--requests", required=True)
    evo.add_argument("--workdir", required=True)
    evo.add_argument("--batch-size", type=int, default=20)
    evo.set_defaults(func=cmd_evolve)

    inj = sub.add_parser("inject", help="inject one of the three required faults")
    inj.add_argument("--kind", choices=sorted(INJECTIONS), required=True)
    inj.add_argument("--routes", required=True)
    inj.add_argument("--tokens", required=True)
    inj.add_argument("--workdir", required=True)
    inj.set_defaults(func=cmd_inject)

    serve = sub.add_parser("serve", help="run the loopback TCP JSON-lines server")
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--routes", required=True)
    serve.add_argument("--tokens", required=True)
    serve.add_argument("--workdir", required=True)
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BusinessError as exc:
        print(json.dumps({"code": exc.code, "message": exc.message,
                          "file": exc.file, "line": exc.line}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
