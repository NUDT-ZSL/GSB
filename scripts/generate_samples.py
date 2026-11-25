#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from opverify.samples import generate_sample_set


if __name__ == "__main__":
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "data" / "seed-sample"
    manifest = generate_sample_set(output)
    print(f"generated {manifest['task_count']} tasks in {output}")
