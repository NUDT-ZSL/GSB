#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h2diag.samplecheck import verify_samples  # noqa: E402


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples"
    report = verify_samples(root)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
