#!/usr/bin/env python3
from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploylab.bundle import write_bundle
from deploylab.demo import DemoRunner


def main() -> int:
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    bundle_path = ROOT / "data" / "sample"
    if not (bundle_path / "manifest.jsonl").exists():
        write_bundle(bundle_path, force=True)
    started = time.time()
    test_run = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    demo = DemoRunner(
        bundle_path=bundle_path,
        work_dir=artifacts / "verification_demo",
    ).run()
    test_output = test_run.stdout + test_run.stderr
    ran_match = re.search(r"Ran (\d+) tests?", test_output)
    result_match = re.search(r"^(OK|FAILED.*)$", test_output, re.MULTILINE)
    report = {
        "ok": test_run.returncode == 0 and bool(demo.get("ok")),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "numpy": np.__version__,
        },
        "tests": {
            "command": f"{sys.executable} -m unittest discover -v",
            "returncode": test_run.returncode,
            "test_count": int(ran_match.group(1)) if ran_match else None,
            "result": result_match.group(1) if result_match else None,
            "stderr": test_run.stderr[-20000:],
            "stdout": test_run.stdout,
        },
        "demo": demo,
        "elapsed_seconds": round(time.time() - started, 3),
    }
    output = artifacts / "verification_report.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "tests_returncode": test_run.returncode,
                "demo_ok": demo.get("ok"),
                "crash_exit": demo["crash_recovery"]["forced_exit_code"],
                "prediction_bytes_equal": demo["crash_recovery"][
                    "prediction_bytes_equal"
                ],
                "activation_checksums_equal": demo["crash_recovery"][
                    "activation_checksums_equal"
                ],
                "affected_layers": demo["revision_update"]["affected_layers"],
                "reused_layers": demo["revision_update"]["reused_layers"],
                "report": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

