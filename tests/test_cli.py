from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "capbench", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_run_status_report_verify_and_text_views(tmp_path: Path) -> None:
    db = tmp_path / "cli.sqlite"
    report = tmp_path / "cli.json"
    run = _cli(
        "run",
        "--scenario",
        str(ROOT / "scenarios" / "demo.json"),
        "--db",
        str(db),
        "--report",
        str(report),
        "--transport",
        "simulated",
    )
    assert run.returncode == 0, run.stderr
    assert report.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["report_schema_version"] == 1
    tenant_names = [item["tenant"] for item in payload["tenants"]]
    assert tenant_names == sorted(tenant_names)

    status_json = _cli("status", "--db", str(db), "--format", "json")
    assert status_json.returncode == 0
    status = json.loads(status_json.stdout)
    assert status["status"] == "completed"
    assert len(status["units"]) == 30

    status_text = _cli("status", "--db", str(db), "--format", "text")
    assert status_text.returncode == 0
    assert "alpha g1 w0" in status_text.stdout

    report_text = _cli("report", "--db", str(db), "--format", "text")
    assert report_text.returncode == 0
    assert "capacity=" in report_text.stdout
    assert "evidence:" in report_text.stdout

    verify = _cli("verify", "--db", str(db), "--report", str(report))
    assert verify.returncode == 0
    assert json.loads(verify.stdout)["ok"] is True


def test_missing_database_has_business_error() -> None:
    result = _cli("status", "--db", "/tmp/definitely-no-capbench.sqlite")
    assert result.returncode == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "E_DB_MISSING"

