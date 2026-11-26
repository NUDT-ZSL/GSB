#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from truth_ledger.sample import FIXED_SEED, generate_task_workspace  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic truth-ledger samples")
    parser.add_argument("--out", default=str(ROOT / "samples"))
    parser.add_argument("--cases", type=int, default=2)
    parser.add_argument("--seed", type=int, default=FIXED_SEED)
    args = parser.parse_args()
    manifest = generate_task_workspace(args.out, case_count=args.cases, seed=args.seed)
    print(
        json.dumps(
            {
                "ok": True,
                "fixed_seed": args.seed,
                "case_count": manifest["case_count"],
                "task_manifest": str(Path(args.out) / "task_manifest.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
