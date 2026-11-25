from __future__ import annotations

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from llm_sampling_diag.cli import main


if __name__ == "__main__":
    exit_code = main([
        "demo",
        "--suite",
        str(root / "samples"),
        "--out",
        str(root / "artifacts" / "demo"),
    ])
    report = root / "artifacts" / "demo" / "demo_report.json"
    if report.exists():
        payload = json.loads(report.read_text(encoding="utf-8"))
        print(json.dumps(payload["acceptance"], indent=2))
    raise SystemExit(exit_code)

