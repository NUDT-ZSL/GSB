#!/usr/bin/env python3
"""Fixed deterministic sample generator entry point."""

from __future__ import annotations

import argparse
import json

from sensitivity_engine.seed import write_sample_workspace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--truth-file", default="truth/expected.json")
    args = parser.parse_args()
    result = write_sample_workspace(
        args.root, data_dir=args.data_dir, truth_file=args.truth_file
    )
    print(json.dumps({"code": "OK", **result}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
