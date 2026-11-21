"""Cross-platform demo launcher.

Usage:
    python scripts/run_demo.py [--workdir artifacts/demo]
"""

from __future__ import annotations

import sys

from netdiag.cli import main


if __name__ == "__main__":
    argv = ["demo"]
    if len(sys.argv) > 1:
        argv.extend(sys.argv[1:])
    raise SystemExit(main(argv))
