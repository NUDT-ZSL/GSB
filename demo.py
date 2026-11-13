#!/usr/bin/env python3
"""Demo driver: build deterministic samples, scan them, print report + summary.

Run from the project directory:

    py demo.py
    py demo.py --workdir out --report out/report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import demo_samples
import pdf_preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workdir",
        default="demo_workspace",
        help="directory under which samples/report are created",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="report path (default: <workdir>/report.json)",
    )
    args = parser.parse_args(argv)

    workdir = Path(args.workdir)
    incoming = demo_samples.write_demo_tree(workdir)
    report_path = Path(args.report) if args.report else workdir / "report.json"

    print(f"[1/2] 已构造确定性样本: {incoming}")
    for sample in sorted(incoming.rglob("*")):
        if sample.is_file():
            print(f"      {sample.relative_to(workdir)}")

    exit_code = pdf_preflight.main([str(incoming), "-o", str(report_path)])
    if exit_code != 0:
        return exit_code

    report = json.loads(report_path.read_text(encoding="utf-8"))
    print("\n[2/2] 报告摘要")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print("\n按等级排序的文件（high -> medium -> low -> unknown）:")
    for entry in report["files"]:
        features = ",".join(entry["features"])
        extra = f", reason={entry['reason']}" if "reason" in entry else ""
        print(
            f"  {entry['level']:<7} {entry['path']:<22} "
            f"features=[{features}]{extra} action={entry['action']}"
        )
    print(f"\n完整报告: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
