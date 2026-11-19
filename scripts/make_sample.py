#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latentflow.imaging import make_reference_frame, save_ppm  # noqa: E402


def main() -> int:
    width, height = 192, 108
    output = ROOT / "samples" / "reference.ppm"
    save_ppm(output, make_reference_frame(width, height))
    print(f"wrote {output} ({width}x{height} RGB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
