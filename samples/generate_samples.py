"""Generate the fixed-seed, offline sample bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from repairflow.samples import (  # noqa: E402
    DEFAULT_HEIGHT,
    DEFAULT_SEED,
    DEFAULT_WIDTH,
    write_sample_bundle,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "samples" / "data"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    args = parser.parse_args()
    manifest = write_sample_bundle(
        args.output,
        seed=args.seed,
        width=args.width,
        height=args.height,
    )
    print(json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
