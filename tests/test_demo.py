from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_repeatable_demo_outputs_are_identical() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["identical"] is True
    assert payload["run1_sha256"] == payload["run2_sha256"]
    assert (ROOT / payload["run1"]).read_bytes() == (
        ROOT / payload["run2"]
    ).read_bytes()

