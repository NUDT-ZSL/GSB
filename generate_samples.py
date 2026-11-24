"""Convenience entry point for local deterministic sample generation."""

from __future__ import annotations

import argparse
import json
import sys

from relief_tiles.errors import BusinessError
from relief_tiles.samples import generate_samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="samples")
    parser.add_argument("--seed", type=int, default=20240517)
    args = parser.parse_args()
    try:
        result = generate_samples(args.output, seed=args.seed)
    except BusinessError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return exc.exit_code
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
