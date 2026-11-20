#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grayroll.samples import generate_samples
from grayroll.util import canonical_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT / "samples",
        help="output directory (replaced atomically by a fresh sample set)",
    )
    arguments = parser.parse_args()
    summary = generate_samples(arguments.root.resolve())
    print(canonical_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
