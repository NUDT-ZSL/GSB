#!/usr/bin/env python3
"""Run the deterministic local acceptance verification and persist the report."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kfdiff.acceptance import run_acceptance, write_report  # noqa: E402


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    report = run_acceptance(root, reset_cache="--no-reset" not in sys.argv)
    records = Path(root) / "records"
    path = write_report(report, records)
    print(f"acceptance {'PASS' if report['overall_pass'] else 'FAIL'}: "
          f"{report['passed_count']}/{report['assertion_count']} in "
          f"{report['elapsed_seconds']}s")
    for assertion in report["assertions"]:
        print(f"  [{'PASS' if assertion['passed'] else 'FAIL'}] {assertion['name']}")
    print(f"report: {path}")
    raise SystemExit(0 if report["overall_pass"] else 1)
