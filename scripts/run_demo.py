"""Convenience script that is safe under the Windows spawn start method."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparse_recall.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["demo", "--fresh", *sys.argv[1:]]))
