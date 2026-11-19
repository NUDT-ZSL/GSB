"""Command line interface for the local VM pool arbiter."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .errors import E_USAGE, BusinessError
from .fixed import canonical_json
from .model import load_sample
from .store import Store


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BusinessError(E_USAGE, message, {"parser": self.prog})


def _add_db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite state file (default: ./vmpool.db)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="vmpool",
        description="Deterministic local VM pool capacity/cost arbiter",
    )
    parser.add_argument(
        "--db", default=None, help="SQLite state file (default: ./vmpool.db)"
    )
    parser.add_argument("--version", action="version", version="vmpool 1.0.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load a sample")
    load_parser.add_argument("--sample", required=True, help="sample directory")
    _add_db_option(load_parser)

    apply_parser = subparsers.add_parser("apply", help="apply request rounds")
    apply_parser.add_argument(
        "--sample",
        default=None,
        help="optional sample directory; otherwise use the loaded scenario",
    )
    apply_parser.add_argument(
        "--seq",
        type=int,
        default=None,
        help="apply all missing requests through this request sequence",
    )
    apply_parser.add_argument(
        "--fault-point",
        choices=("round-started", "work-written"),
        default=None,
        help=argparse.SUPPRESS,
    )
    _add_db_option(apply_parser)

    for name, help_text in (
        ("diagnose", "show precision, error chains and invalidations"),
        ("resume", "finish durable in-progress rounds"),
        ("status", "show the complete committed snapshot"),
        ("verify", "verify snapshot, pool, budget and derived state"),
    ):
        child = subparsers.add_parser(name, help=help_text)
        _add_db_option(child)
    return parser


def _db_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "db", None) or "vmpool.db")


def invoke(args: argparse.Namespace) -> dict:
    if args.command == "load":
        scenario = load_sample(args.sample, require_manifest=True)
        return Store(_db_path(args)).load(scenario)

    store = Store(_db_path(args))
    if args.command == "apply":
        scenario = (
            load_sample(args.sample, require_manifest=True)
            if args.sample
            else store.get_scenario()
        )
        if args.seq is not None and args.seq <= 0:
            raise BusinessError(
                E_USAGE,
                "--seq 必须是正整数",
                {"seq": args.seq},
            )
        return store.apply(
            scenario,
            through_seq=args.seq,
            fault_point=args.fault_point,
        )
    if args.command == "status":
        return store.status()
    if args.command == "diagnose":
        return store.diagnose()
    if args.command == "verify":
        return store.verify()
    if args.command == "resume":
        return store.resume()
    raise BusinessError(E_USAGE, "未知子命令", {"command": args.command})


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        result = invoke(args)
        print(canonical_json(result))
        return 0
    except BusinessError as exc:
        print(canonical_json(exc.to_dict()))
        return exc.exit_code
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
