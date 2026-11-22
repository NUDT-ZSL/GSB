#!/usr/bin/env python3
"""Run the SIGKILL/resume acceptance demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparse_inc.cli import _recovery_demo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="samples/cases.jsonl")
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--runs", default="runs")
    args = parser.parse_args()
    reports = Path(args.reports)
    runs = Path(args.runs)
    reports.mkdir(parents=True, exist_ok=True)
    runs.mkdir(parents=True, exist_ok=True)
    result = _recovery_demo(
        args.manifest,
        "spmm_inc_demo",
        reports,
        runs,
    )
    output = reports / "recovery_demo.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    checks = [
        result["forced_termination"]["sigkill_observed"],
        result["forced_termination"]["uncommitted_artifact_left"],
        result["resume"]["discarded_pending"],
        result["resume"]["pending_artifact_removed"],
        result["equivalence"]["checkpoint_bytes_equal"],
        result["equivalence"]["ledger_equal"],
        result["equivalence"]["result_equal"],
    ]
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
