from __future__ import annotations

import json
import platform
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    suite = unittest.defaultTestLoader.discover(
        str(root / "tests"),
        top_level_dir=str(root),
    )
    tests = [
        test.id()
        for test in _iterate(suite)
        if test is not None and hasattr(test, "id")
    ]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "numpy": np.__version__,
        "tests_run": result.testsRun,
        "tests": tests,
        "failures": [str(item) for item in result.failures],
        "errors": [str(item) for item in result.errors],
        "skipped": [str(item) for item in result.skipped],
        "success": result.wasSuccessful(),
    }
    destination = root / "artifacts" / "validation_report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(destination),
                "tests_run": result.testsRun,
                "success": result.wasSuccessful(),
            },
            indent=2,
        )
    )
    return 0 if result.wasSuccessful() else 1


def _iterate(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iterate(item)
        else:
            yield item


if __name__ == "__main__":
    raise SystemExit(main())
