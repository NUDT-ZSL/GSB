from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rsseg.sequence import run_edit_sequence


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply deterministic edits and verify incremental vs full")
    parser.add_argument("--workspace", default=str(ROOT / "sample"))
    parser.add_argument("--state", default=str(ROOT / "states" / "sequence"))
    parser.add_argument("--edits", default=str(ROOT / "sample" / "edits.json"))
    parser.add_argument("--report", default=str(ROOT / "artifacts" / "sequence_report.json"))
    parser.add_argument("--max-batches-per-edit", type=int, default=100)
    parser.add_argument("--keep-state", action="store_true")
    args = parser.parse_args()
    report = run_edit_sequence(
        args.workspace,
        args.state,
        args.edits,
        report_path=args.report,
        max_batches_per_edit=args.max_batches_per_edit,
        reset_state=not args.keep_state,
    )
    print(json.dumps({"all_equal": report["all_equal"], "steps": len(report["steps"])}, ensure_ascii=False))
    return 0 if report["all_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
