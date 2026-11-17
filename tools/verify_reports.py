from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fixed_join.cli import main


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] != "verify":
        argv = ["verify", *argv]
    raise SystemExit(main(argv))
