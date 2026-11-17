#!/usr/bin/env python3
"""Generate the complete deterministic sample workspace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from replica_ledger.samples import generate_samples  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "samples"),
        help="sample output directory",
    )
    parser.add_argument("--force", action="store_true", help="replace generated content")
    args = parser.parse_args()
    index = generate_samples(args.output, force=args.force)
    print(json_dumps(index))
    return 0


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
