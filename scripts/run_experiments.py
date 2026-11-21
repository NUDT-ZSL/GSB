#!/usr/bin/env python3
"""Convenience wrapper around the reproducible experiment runner."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from query_understanding.experiments import run_experiments  # noqa: E402


if __name__ == "__main__":
    report = run_experiments(ROOT / "data", ROOT / "reports")
    print(
        {
            "ok": report["verification"]["all_passed"],
            "report_core_checksum": report["report_core_checksum"],
        }
    )
    raise SystemExit(0 if report["verification"]["all_passed"] else 1)

