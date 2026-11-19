#!/usr/bin/env python3
"""Run the one-shot demo from a source checkout."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dialogue_risk.demo import run_demo  # noqa: E402


def main() -> int:
    report = run_demo(ROOT / "data")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

