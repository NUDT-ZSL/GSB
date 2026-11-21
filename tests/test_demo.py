from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.conftest import run_cli


def test_fixed_demo_reports_are_byte_identical(tmp_path: Path) -> None:
    workdir = tmp_path / "demo"
    result = run_cli(
        [
            "demo",
            "--workdir",
            str(workdir),
        ]
    )
    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    assert summary["byte_identical"] is True
    report_a = (workdir / "report-run-1.json").read_bytes()
    report_b = (workdir / "report-run-2.json").read_bytes()
    assert report_a == report_b
    assert hashlib.sha256(report_a).hexdigest() == summary["report_sha256"]
    verify = json.loads(
        run_cli(
            ["--db", str(workdir / "demo-run-1.db"), "verify"]
        ).stdout
    )
    assert verify["ok"] is True
    assert verify["checks"] > 100
