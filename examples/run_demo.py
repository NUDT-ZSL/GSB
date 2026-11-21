from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dep_arc_interop.demo import run_demo, write_report

if __name__ == "__main__":
    report = run_demo(ROOT / "data")
    write_report(report, ROOT / "artifacts" / "validation_report.json")
    print(
        json.dumps(
            {
                "all_passed": report["all_passed"],
                "equivalence": report["equivalence"],
                "selfcheck": report["selfcheck"],
                "report": str(ROOT / "artifacts" / "validation_report.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
