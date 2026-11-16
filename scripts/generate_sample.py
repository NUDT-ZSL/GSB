from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rsseg.sample import generate_sample


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local multiband RS tiles")
    parser.add_argument("--out", default=str(ROOT / "sample"))
    parser.add_argument("--seed", default=20261117, type=int)
    args = parser.parse_args()
    result = generate_sample(args.out, args.seed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
