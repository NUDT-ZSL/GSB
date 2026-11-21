from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .demo import run_demo, write_demo_outputs
from .generation import write_samples
from .server import serve
from .util import canonical_json_pretty


def _workspace(value: str) -> Path:
    return Path(value).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cqir",
        description="Deterministic conversational retrieval and citation interop",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="write deterministic local samples")
    generate.add_argument("--root", default=".")
    generate.add_argument("--seed", type=int, default=20261122)

    demo = subparsers.add_parser("demo", help="run the complete repeatable demo")
    demo.add_argument("--root", default=".")
    demo.add_argument("--conv-id", default="conv-002")
    demo.add_argument("--turn-no", type=int, default=1)
    demo.add_argument("--seed", type=int, default=20261122)
    demo.add_argument("--json", action="store_true", help="print the complete JSON result")

    verify = subparsers.add_parser("verify", help="run demo and fail if any check fails")
    verify.add_argument("--root", default=".")
    verify.add_argument("--seed", type=int, default=20261122)

    server = subparsers.add_parser("serve", help="start the local interactive console")
    server.add_argument("--root", default=".")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = _workspace(args.root)
    if args.command == "generate":
        print(canonical_json_pretty(write_samples(workspace / "samples", args.seed)))
        return 0
    if args.command in ("demo", "verify"):
        result = run_demo(
            workspace,
            getattr(args, "conv_id", "conv-002"),
            getattr(args, "turn_no", 1),
            args.seed,
        )
        outputs = write_demo_outputs(workspace, result)
        if getattr(args, "json", False):
            print(canonical_json_pretty(result))
        else:
            summary = {
                "all_passed": result["all_passed"],
                "checks": result["checks"],
                "baseline_byte_hash": result["baseline"]["byte_hash"],
                "incremental_byte_hash": result["incremental"]["byte_hash"],
                "fresh_byte_hash": result["incremental"]["fresh_byte_hash"],
                "snapshot_versions": result["snapshots"]["versions"],
                "error_codes": sorted(
                    {item["code"] for item in result["error_journal"]}
                ),
                "outputs": outputs,
            }
            print(canonical_json_pretty(summary))
        if args.command == "verify" and not result["all_passed"]:
            return 1
        return 0
    if args.command == "serve":
        serve(workspace, args.host, args.port)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
