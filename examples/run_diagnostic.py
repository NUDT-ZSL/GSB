#!/usr/bin/env python3
"""Complete integration example: generate, validate, adjudicate, refresh and report."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from transcript_governance.diagnose import run_diagnostic  # noqa: E402


if __name__ == "__main__":
    report = run_diagnostic(ROOT / "samples", ROOT / "reports")
    print(Path(report["markdown_report"]).read_text(encoding="utf-8"))
    raise SystemExit(0 if report["overall_ok"] else 1)
