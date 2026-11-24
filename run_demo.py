"""Run the complete deterministic offline demonstration."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from sso_ticket_guard.samplegen import DEFAULT_SEED, generate_samples

ROOT = Path(__file__).resolve().parent


def run_cli(*arguments: str) -> int:
    result = subprocess.run(
        [sys.executable, "-m", "sso_ticket_guard.cli", *arguments],
        cwd=ROOT,
        check=False,
    )
    return result.returncode


def main() -> int:
    generate_samples(ROOT / "samples", DEFAULT_SEED)
    commands = [
        (
            "import",
            "import",
            "--samples",
            "samples",
            "--output",
            "reports/import.json",
        ),
        (
            "renew",
            "renew",
            "--samples",
            "samples",
            "--output",
            "reports/renew.json",
            "--state-dir",
            ".sso-state",
            "--fresh",
        ),
        (
            "revoke",
            "revoke",
            "--samples",
            "samples",
            "--output",
            "reports/revoke.json",
            "--state-dir",
            ".sso-state",
        ),
        (
            "checkpoint",
            "checkpoint",
            "verify",
            "--samples",
            "samples",
            "--state-dir",
            ".sso-state",
            "--output",
            "reports/checkpoint.json",
        ),
    ]
    for label, *arguments in commands:
        exit_code = run_cli(*arguments)
        allowed = {0, 1}
        if exit_code not in allowed:
            print(f"{label} failed with argument error exit={exit_code}")
            return 1

    truth = json.loads((ROOT / "samples" / "truth.json").read_text(encoding="utf-8"))
    renew = json.loads((ROOT / "reports" / "renew.json").read_text(encoding="utf-8"))
    revoke = json.loads((ROOT / "reports" / "revoke.json").read_text(encoding="utf-8"))
    checkpoint = json.loads(
        (ROOT / "reports" / "checkpoint.json").read_text(encoding="utf-8")
    )
    decision_by_id = {
        item["request_id"]: (item["decision"], item["code"])
        for item in renew["adjudications"]
    }
    expected = {
        request_id: (item["decision"], item["code"])
        for request_id, item in truth["requests"].items()
    }
    revocation_by_id = {
        item["request_id"]: item for item in revoke["revocations"]
    }
    checks = [
        decision_by_id == expected,
        revocation_by_id["rev-dev-chain"]["ticket_ids"]
        == truth["revocations"]["rev-dev-chain"]["ticket_ids"],
        revocation_by_id["rev-cycle"]["cycle_ticket_ids"]
        == truth["revocations"]["rev-cycle"]["cycle_ticket_ids"],
        checkpoint["checkpoint"]["watermark"] == 5,
        checkpoint["checkpoint"]["diagnostics"]["replay_verified"] is True,
    ]
    if not all(checks):
        print("demo truth synchronization failed")
        return 1
    print(
        "demo ok:",
        renew["summary"],
        "revocation_requests=",
        len(revoke["revocations"]),
        "checkpoint_watermark=",
        checkpoint["checkpoint"]["watermark"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

