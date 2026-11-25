"""Command-line entry point: ``python -m drift_ae replay <file>``."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m drift_ae {replay} [args...]", file=sys.stderr)
        return 64
    command, rest = args[0], args[1:]
    if command == "replay":
        from .replay import main as replay_main

        return replay_main(rest)
    print(f"unknown command: {command}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
