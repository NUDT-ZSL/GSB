#!/usr/bin/env python3
"""Backward-compatible wrapper for ``python -m coref_pipeline.cli``."""

import sys

from coref_pipeline.cli import main


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if not arguments or arguments[0] not in {"run", "generate", "verify"}:
        arguments = ["run", *arguments]
    raise SystemExit(main(arguments))
