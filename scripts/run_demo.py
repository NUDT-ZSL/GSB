#!/usr/bin/env python3
"""Run the full deterministic OCR diagnostic demo and print the report."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocrbench.demo import run_demo


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic OCR workbench demo")
    parser.add_argument(
        "--workspace",
        default=os.path.join(os.getcwd(), "workspace_demo"),
        help="Workspace directory for data, cache and snapshots",
    )
    args = parser.parse_args()
    report = run_demo(args.workspace)
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"\nDemo report written to {os.path.join(args.workspace, 'demo_report.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
