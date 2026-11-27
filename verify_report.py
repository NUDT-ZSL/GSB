#!/usr/bin/env python3
"""Independent final-report rule checker.

This command accepts only the final JSON report and plain block feature table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from layout_analyzer.verifier import verify_from_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="final page report JSON")
    parser.add_argument("--features", required=True, help="plain block feature table JSON")
    parser.add_argument("--out", help="optional verifier result JSON path")
    args = parser.parse_args(argv)
    result = verify_from_files(args.report, args.features)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
