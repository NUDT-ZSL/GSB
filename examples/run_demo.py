"""Minimal integration example.

Run from the repository root:

    python examples/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from timbre_lab.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(
        main(
            [
                "demo",
                "--workdir",
                str(ROOT / "artifacts" / "demo"),
                "--inject-errors",
            ]
        )
    )
