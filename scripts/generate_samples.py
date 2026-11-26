#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dialog_state_engine.canonical import canonical_line
from dialog_state_engine.samples import DEFAULT_SEED, generate_samples


def main() -> int:
    manifest = generate_samples(ROOT / "samples", seed=DEFAULT_SEED)
    sys.stdout.write(canonical_line(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

