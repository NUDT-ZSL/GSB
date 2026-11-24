from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sessionfix.samplegen import DEFAULT_SEED, generate_sample


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the deterministic offline session-fixation sample."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--sample-id")
    args = parser.parse_args()
    summary = generate_sample(
        args.output,
        seed=args.seed,
        sample_id=args.sample_id,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
