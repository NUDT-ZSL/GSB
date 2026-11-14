"""Write the deterministic sample dataset (default: ./samples)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python tools/generate_samples.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lightmap_seams.samples import build_sample_dataset  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "samples",
        help="Directory to (re)create the sample dataset in.",
    )
    args = parser.parse_args()
    settings = build_sample_dataset(args.destination)
    print(f"sample settings: {settings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
