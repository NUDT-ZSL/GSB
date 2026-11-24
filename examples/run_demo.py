#!/usr/bin/env python3
"""Runnable library example using the fixed-seed sample."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from certpath import CertPathEngine


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default="samples")
    parser.add_argument("--workspace", default="artifacts/demo-state")
    parser.add_argument("--report", default="artifacts/demo-report.json")
    parser.add_argument("--order", default="v1,v2")
    args = parser.parse_args()
    order = tuple(item.strip() for item in args.order.split(","))
    engine = CertPathEngine(args.workspace)
    report = engine.adjudicate(args.samples, arrival_order=order)  # type: ignore[arg-type]
    engine.export_report(args.report)
    print(
        json.dumps(
            {
                "report": args.report,
                "summary": report["summary"],
                "conflicts": len(report["snapshot"]["conflicts"]),
                "checkpoint": engine.verify_checkpoint(recover=True),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
