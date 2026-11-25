#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from radapt.sample import FIXED_GLOBAL_SEED, write_sample_files


if __name__ == "__main__":
    output = ROOT / "samples" / "deterministic"
    result = write_sample_files(output, FIXED_GLOBAL_SEED)
    print(f"generated {result['batch_count']} deterministic batches in {result['out_dir']}")
    print(f"global_seed={FIXED_GLOBAL_SEED}")
