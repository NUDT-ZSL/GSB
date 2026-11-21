"""Cross-platform deterministic demonstration for the local diagnostic tool."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEMO = ROOT / "demo"


def run(*args: str) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        [sys.executable, "-m", "lockdiag", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed: {args}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def main() -> int:
    for name in ("sample", "run-a", "run-b", "reports"):
        target = DEMO / name
        if target.exists():
            shutil.rmtree(target)
    (DEMO / "reports").mkdir(parents=True, exist_ok=True)

    run("generate", "--out", str(DEMO / "sample"))
    run("run", "--sample", str(DEMO / "sample"), "--state", str(DEMO / "run-a"))
    run("run", "--sample", str(DEMO / "sample"), "--state", str(DEMO / "run-b"))

    report_a = DEMO / "reports" / "report-a.json"
    report_b = DEMO / "reports" / "report-b.json"
    run("report", "--state", str(DEMO / "run-a"), "--out", str(report_a))
    run("report", "--state", str(DEMO / "run-b"), "--out", str(report_b))

    left = report_a.read_bytes()
    right = report_b.read_bytes()
    verify = json.loads(run("verify", "--state", str(DEMO / "run-a")).stdout)
    report = json.loads(left)
    summary = {
        "ok": left == right and verify["ok"],
        "reports_byte_identical": left == right,
        "report_sha256": hashlib.sha256(left).hexdigest(),
        "verify_ok": verify["ok"],
        "transaction_statuses": {
            item["tx_id"]: item["status"] for item in report["transactions"]
        },
        "wait_count": report["summary"]["resolved_wait_count"],
        "wait_total_ms": report["summary"]["wait_total_ms"],
        "error_bound_ms": report["summary"]["error_bound_ms"],
        "precision_sensitive_count": report["summary"]["precision_sensitive_count"],
        "deadlock_count": report["summary"]["deadlock_count"],
        "attribution_triples": report["summary"]["attribution_triples"],
        "index_paths": report["summary"]["index_paths"],
        "outputs": {
            "sample": str(DEMO / "sample"),
            "state_a": str(DEMO / "run-a"),
            "state_b": str(DEMO / "run-b"),
            "report_a": str(report_a),
            "report_b": str(report_b),
        },
    }
    (DEMO / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
