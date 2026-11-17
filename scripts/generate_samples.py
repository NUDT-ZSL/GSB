#!/usr/bin/env python3
"""Generate the deterministic local SLP sample corpus."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slpguard.samples import generate_samples  # noqa: E402


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples"
    manifest = generate_samples(output)
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "samples": len(manifest["samples"]),
                "manifest": str((output / "manifest.json").resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
