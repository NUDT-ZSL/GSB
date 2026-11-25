#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gemm_bmm_workbench.sampling import generate_sample_tree


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic GEMM/BMM sample jobs and arrays")
    parser.add_argument("--out", default=str(ROOT / "samples"))
    args = parser.parse_args()
    manifest = generate_sample_tree(args.out)
    print(f"generated {manifest['job_count_batch_1']} + {manifest['job_count_batch_2']} jobs in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
