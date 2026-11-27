#!/usr/bin/env python3
"""Fixed batch entry point.

Default invocation:
    python3 run_batch.py

Equivalent to:
    python3 -m layout_analyzer.cli all --seed 424242 \
        --samples artifacts/samples --out artifacts/results
"""

import sys

from layout_analyzer.cli import main


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] not in {"generate", "analyze", "all"}:
        argv.insert(0, "all")
    raise SystemExit(main(argv))
