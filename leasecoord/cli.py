from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import read_json
from .db import answer_json
from .engine import Engine
from .errors import BusinessError


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        error = BusinessError(
            "E_CLI_USAGE",
            "Invalid command-line arguments",
            details={"reason": message},
        )
        print(answer_json(error.as_dict()))
        raise SystemExit(2)


def _crash_choice(value: str) -> str:
    allowed = {"after_prepare", "after_final_insert", "after_commit"}
    if value not in allowed:
        raise argparse.ArgumentTypeError(f"expected one of {sorted(allowed)}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="leasectl",
        description="Deterministic local lease election and failover engine",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="Validate and persist fixed local samples")
    load.add_argument("--workspace", default="workspace")
    load.add_argument("--nodes", default=None)
    load.add_argument("--config", default=None)
    load.add_argument("--clock", default="0")
    load.add_argument("--reset", action="store_true")
    load.add_argument("--compact", action="store_true")

    elect = subparsers.add_parser("elect", help="Grant bootstrap leases or elect a leader")
    elect.add_argument("--workspace", default="workspace")
    elect.add_argument("--clock", required=True)
    elect.add_argument("--crash-at", type=_crash_choice, default=None)
    elect.add_argument("--compact", action="store_true")

    renew = subparsers.add_parser("renew", help="Renew a held lease idempotently")
    renew.add_argument("--workspace", default="workspace")
    renew.add_argument("--node", required=True)
    renew.add_argument("--session-epoch", required=True, type=int)
    renew.add_argument("--clock", required=True)
    renew.add_argument("--lease-generation", type=int, default=None)
    renew.add_argument("--data-version", type=int, default=None)
    renew.add_argument("--crash-at", type=_crash_choice, default=None)
    renew.add_argument("--compact", action="store_true")

    resume = subparsers.add_parser("resume", help="Clean half-commits and rebuild election")
    resume.add_argument("--workspace", default="workspace")
    resume.add_argument("--clock", default=None)
    resume.add_argument("--compact", action="store_true")

    status = subparsers.add_parser("status", help="Render stable node and lease state")
    status.add_argument("--workspace", default="workspace")
    status.add_argument("--clock", default=None)
    status.add_argument("--compact", action="store_true")

    verify = subparsers.add_parser("verify", help="Verify persisted state and election")
    verify.add_argument("--workspace", default="workspace")
    verify.add_argument("--compact", action="store_true")
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace)
    engine = Engine(workspace)
    if args.command == "load":
        nodes_path = Path(args.nodes) if args.nodes else workspace / "nodes.json"
        config_path = Path(args.config) if args.config else workspace / "election_config.json"
        return engine.load(
            read_json(nodes_path),
            read_json(config_path),
            clock=args.clock,
            reset=args.reset,
        )
    if args.command == "elect":
        return engine.elect(clock=args.clock, crash_at=args.crash_at)
    if args.command == "renew":
        return engine.renew(
            node=args.node,
            session_epoch=args.session_epoch,
            clock=args.clock,
            lease_generation=args.lease_generation,
            data_version=args.data_version,
            crash_at=args.crash_at,
        )
    if args.command == "resume":
        return engine.resume(clock=args.clock)
    if args.command == "status":
        return engine.status(clock=args.clock)
    if args.command == "verify":
        return engine.verify()
    raise BusinessError("E_CLI_USAGE", "Unknown command", details={"command": args.command})


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
    except BusinessError as exc:
        print(answer_json(exc.as_dict()))
        return 2
    except KeyboardInterrupt:
        print(
            answer_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERRUPTED",
                        "message": "Command interrupted",
                        "details": {},
                    },
                }
            )
        )
        return 130
    print(answer_json(result, compact=bool(getattr(args, "compact", False))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
