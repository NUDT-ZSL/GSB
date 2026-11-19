#!/usr/bin/env python3
"""Fixed-entry script for deterministic data generation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intent_interop.generator import generate_fixtures  # noqa: E402


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"
    manifest = generate_fixtures(out_dir)
    print(
        json.dumps(
            {
                "status": "OK",
                "out_dir": str(out_dir),
                "query_count": manifest["query_count"],
                "file_count": len(manifest["files"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
