from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quantlab.demo import PROJECT_ROOT, run_demo
from quantlab.serialization import atomic_write_json


def main() -> int:
    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    ran = re.search(r"Ran (\d+) tests", tests.stderr)
    test_count = int(ran.group(1)) if ran else 0
    if tests.returncode != 0:
        print(tests.stdout)
        print(tests.stderr, file=sys.stderr)
        return tests.returncode
    demo = run_demo(crash_after_layer=1)
    report = {
        "schema": 1,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.platform(),
        },
        "unit_tests": {
            "command": f"{sys.executable} -m unittest discover -s tests -v",
            "tests_run": test_count,
            "status": "OK",
        },
        "demo": demo,
        "artifacts": {
            "dashboard": str(PROJECT_ROOT / "artifacts" / "dashboard.json"),
            "demo_report": str(PROJECT_ROOT / "artifacts" / "demo_report.json"),
        },
    }
    output = PROJECT_ROOT / "artifacts" / "validation_report.json"
    atomic_write_json(
        output,
        report,
        temp_dir=PROJECT_ROOT / "runtime" / "recovery-tmp",
        indent=2,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
