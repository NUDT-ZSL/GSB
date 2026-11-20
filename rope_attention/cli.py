"""Command line entry points for deterministic local demonstrations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .data import prepare_sample_workspace, verify_tensor_manifest
from .demo import run_demo, run_fault_demo
from .errors import RopeError


def _write_pretty(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def _emit(value: Any, output: str | None) -> None:
    text = _write_pretty(value)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rope-attention",
        description="Deterministic RoPE attention and versioned config component",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="workspace root (default: current directory)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="generate deterministic tasks, configs and Q/K/V arrays",
    )
    prepare.add_argument("--output", default=None)

    demo = subparsers.add_parser(
        "demo",
        help="run version interop, migration, attention and incremental demos",
    )
    demo.add_argument("--output", default=None)

    fault = subparsers.add_parser(
        "fault-demo",
        help="run explicit corrupt-input, corrupt-cache and resource fault injections",
    )
    fault.add_argument("--output", default=None)

    verify = subparsers.add_parser(
        "verify-samples",
        help="verify deterministic Q/K/V sample hashes",
    )
    verify.add_argument("--output", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "prepare":
            manifest = prepare_sample_workspace(root)
            _emit(
                {
                    "business_code": "OK",
                    "ok": True,
                    "output_root": str(root / "samples"),
                    "tensor_manifest_checksum": manifest["checksum"],
                },
                args.output,
            )
        elif args.command == "demo":
            _emit(run_demo(root), args.output)
        elif args.command == "fault-demo":
            _emit(run_fault_demo(root), args.output)
        elif args.command == "verify-samples":
            _emit(
                {
                    "business_code": "OK",
                    "ok": True,
                    "verification": verify_tensor_manifest(root),
                },
                args.output,
            )
        else:
            parser.error("unknown command")
    except RopeError as exc:
        _emit(exc.to_dict(), args.output)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

