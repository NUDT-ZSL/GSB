"""Generate deterministic sample inputs into the data/ directory.

Usage::

    python scripts/generate_data.py [--seed 20261124] [--out data]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xrel.data_gen import DEFAULT_SEED, write_inputs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=str, default=str(ROOT / "data"))
    args = parser.parse_args()

    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = write_inputs(output_dir, seed=args.seed)
    print("generated deterministic inputs:")
    for key, value in manifest["files"].items():
        print(f"  {key}: {value}")
    print(f"  documents={manifest['document_count']} queries={manifest['query_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
