#!/usr/bin/env python3
"""Generate the fixed-seed JSONL manifest used by the example and tests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moe_router.validation import write_cases  # noqa: E402
from moe_router.weights import generate_cases, manifest_rows  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "samples" / "cases.jsonl"))
    parser.add_argument(
        "--manifest", default=str(ROOT / "samples" / "weight_manifest.json")
    )
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--count", type=int, default=6)
    args = parser.parse_args()
    cases = generate_cases(seed=args.seed, count=args.count)
    write_cases(args.out, cases)
    manifest = {"seed": args.seed, "cases": manifest_rows(cases)}
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "generated_cases": len(cases),
                "cases_path": args.out,
                "manifest_path": args.manifest,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
