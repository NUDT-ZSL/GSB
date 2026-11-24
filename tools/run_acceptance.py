from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def run(
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(SRC)
    if env:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "sessionfix", *args],
        cwd=str(ROOT),
        env=process_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def run_json(
    *args: str,
    expected: int = 0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    completed = run(*args, env=env)
    if completed.returncode != expected:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {args}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic end-to-end acceptance scenario."
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sessionfix-acceptance-") as raw:
        root = Path(raw)
        sample = root / "fixed-seed"
        generated = run_json(
            "generate",
            "--output",
            str(sample),
            "--seed",
            "20261125",
        )

        baseline = root / "baseline"
        run_json(
            "import",
            "--sample",
            str(sample),
            "--workdir",
            str(baseline),
        )
        run_json(
            "adjudicate",
            "--workdir",
            str(baseline),
            "--batch-size",
            "2",
        )
        baseline_report = json.loads(
            (baseline / "state" / "report.json").read_text(encoding="utf-8")
        )

        recovered = root / "recovered"
        run_json(
            "import",
            "--sample",
            str(sample),
            "--workdir",
            str(recovered),
        )
        crashed = run(
            "adjudicate",
            "--workdir",
            str(recovered),
            "--batch-size",
            "2",
            env={"SESSIONFIX_TEST_CRASH_AFTER_BATCH": "1"},
        )
        if crashed.returncode != 137:
            raise RuntimeError(f"expected forced exit 137, got {crashed.returncode}")
        no_report_after_crash = not (
            recovered / "state" / "report.json"
        ).exists()
        run_json(
            "adjudicate",
            "--workdir",
            str(recovered),
            "--batch-size",
            "2",
        )
        recovered_report = json.loads(
            (recovered / "state" / "report.json").read_text(encoding="utf-8")
        )

        same_decision = (
            baseline_report["decision_digest"]
            == recovered_report["decision_digest"]
        )
        same_ledger = (
            baseline_report["ledger"]["digest"]
            == recovered_report["ledger"]["digest"]
        )
        same_sessions = baseline_report["sessions"] == recovered_report["sessions"]

        revoked = run_json(
            "revoke",
            "--workdir",
            str(recovered),
            "--session",
            "revoke-a",
        )
        closure = revoked["results"][0]["revocation"]["closure"]
        verification = run_json(
            "verify-checkpoint",
            "--workdir",
            str(recovered),
        )
        checkpoint_result = dict(verification["results"][0])
        checkpoint_result.pop("workdir", None)

        result = {
            "ok": bool(
                same_decision
                and same_ledger
                and same_sessions
                and no_report_after_crash
                and verification["ok"]
                and closure == ["revoke-a", "revoke-b", "revoke-c"]
            ),
            "python": sys.version.split()[0],
            "seed": generated["seed"],
            "sample_id": generated["sample_id"],
            "observed_clock_ms": generated["observed_clock_ms"],
            "baseline": {
                "decision_digest": baseline_report["decision_digest"],
                "ledger_digest": baseline_report["ledger"]["digest"],
                "repair_state_counts": baseline_report["summary"][
                    "repair_state_counts"
                ],
                "business_codes": baseline_report["business_codes"],
                "session_fixation_sessions": baseline_report["summary"][
                    "session_fixation_sessions"
                ],
                "fixed_sessions": baseline_report["summary"]["fixed_sessions"],
                "orphan_sessions": sorted(
                    item["session_id"]
                    for item in baseline_report["sessions"]
                    if "E_ORPHAN_SESSION" in item["codes"]
                ),
                "rotate_too_fast_sessions": sorted(
                    item["session_id"]
                    for item in baseline_report["sessions"]
                    if "E_ROTATE_TOO_FAST" in item["codes"]
                ),
            },
            "recovery": {
                "forced_exit_code": crashed.returncode,
                "report_absent_after_crash": no_report_after_crash,
                "same_decision": same_decision,
                "same_sessions": same_sessions,
                "same_ledger_digest": same_ledger,
                "resume_metadata": recovered_report["ledger"]["recovery"],
            },
            "revocation": {
                "target": "revoke-a",
                "closure": closure,
            },
            "checkpoint_verification": checkpoint_result,
        }
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
