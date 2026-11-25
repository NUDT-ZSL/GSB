#!/usr/bin/env python3
"""End-to-end acceptance run using only local deterministic samples."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run(
    *args: str,
    check: bool = True,
    expected_rc: int = 0,
) -> tuple[dict[str, Any], int]:
    result = subprocess.run(
        [PYTHON, "-m", "topodiag", *args],
        cwd=str(ROOT),
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT)
            + (
                os.pathsep + os.environ["PYTHONPATH"]
                if os.environ.get("PYTHONPATH")
                else ""
            ),
        },
        text=True,
        capture_output=True,
        timeout=120,
    )
    payload: dict[str, Any] = {}
    if result.stdout.strip():
        payload = json.loads(result.stdout)
    if check and (result.returncode != expected_rc or not payload.get("ok")):
        raise RuntimeError(
            f"command failed: {' '.join(args)}\n"
            f"rc={result.returncode}\n"
            f"stdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
    return payload, result.returncode


def prepare(workdir: Path) -> None:
    run("load", "--workdir", str(workdir))
    run("build", "--workdir", str(workdir))


def status(workdir: Path) -> dict[str, Any]:
    return run("status", "--workdir", str(workdir))[0]


def verify(workdir: Path) -> dict[str, Any]:
    payload, _ = run("verify", "--workdir", str(workdir))
    if not payload["ok"]:
        raise RuntimeError(f"verify failed: {payload['errors']}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--report",
        default=str(ROOT / "artifacts" / "acceptance_report.json"),
    )
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="topodiag-acceptance-"))
    baseline = root / "baseline"
    updated = root / "updated"
    resource_crash = root / "resource-crash"
    policy_crash = root / "policy-crash"
    dependency_crash = root / "dependency-crash"
    report: dict[str, Any] = {
        "ok": False,
        "python": sys.version.split()[0],
        "root": str(root),
        "commands": [],
        "scenarios": {},
    }
    try:
        for workdir in (
            baseline,
            updated,
            resource_crash,
            policy_crash,
            dependency_crash,
        ):
            prepare(workdir)
        report["commands"].extend(["load", "build"])

        schedule, _ = run(
            "schedule",
            "--workdir",
            str(baseline),
            "--round-budget-ms",
            "250",
            "--workers",
            "4",
        )
        report["commands"].append("schedule")
        baseline_status = status(baseline)
        baseline_verify = verify(baseline)
        report["commands"].extend(["status", "verify"])
        report["scenarios"]["baseline"] = {
            "summary": schedule["summary"],
            "semantic_digest": baseline_status["semantic_digest"],
            "verify_ok": baseline_verify["ok"],
        }

        repeated, _ = run(
            "schedule",
            "--workdir",
            str(baseline),
            "--round-budget-ms",
            "250",
        )
        repeated_status = status(baseline)
        report["scenarios"]["idempotent_reschedule"] = {
            "semantic_digest_unchanged": (
                baseline_status["semantic_digest"]
                == repeated_status["semantic_digest"]
            ),
            "summary_unchanged": schedule["summary"] == repeated["summary"],
        }

        update_build, _ = run(
            "build",
            "--workdir",
            str(updated),
            "--policy-package",
            str(updated / "samples" / "policies" / "v2-update"),
        )
        update_schedule, _ = run(
            "schedule",
            "--workdir",
            str(updated),
            "--round-budget-ms",
            "250",
        )
        updated_status = status(updated)
        updated_verify = verify(updated)
        update_events = {
            event["trigger"]["template_name"]: event
            for event in update_build["invalidations"]
        }
        report["scenarios"]["policy_update"] = {
            "mode": update_build["mode"],
            "generation": updated_status["policy_pointer"]["policy_generation"],
            "analytics_affected_downstream": update_events["analytics"][
                "affected_downstream"
            ],
            "orders_unaffected": next(
                item
                for item in updated_status["closures"]
                if item["template_name"] == "orders"
            )["revision"]
            == next(
                item
                for item in baseline_status["closures"]
                if item["template_name"] == "orders"
            )["revision"],
            "summary": update_schedule["summary"],
            "semantic_digest": updated_status["semantic_digest"],
            "verify_ok": updated_verify["ok"],
        }

        crash, crash_rc = run(
            "schedule",
            "--workdir",
            str(resource_crash),
            "--round-budget-ms",
            "250",
            "--crash-at",
            "resource",
            check=False,
        )
        if crash_rc != 79:
            raise RuntimeError(f"resource crash returned {crash_rc}, expected 79")
        recover, _ = run(
            "recover",
            "--workdir",
            str(resource_crash),
            "--round-budget-ms",
            "250",
        )
        resource_status = status(resource_crash)
        resource_verify = verify(resource_crash)
        report["commands"].append("recover")
        report["scenarios"]["resource_crash_recovery"] = {
            "crash_exit": crash_rc,
            "recovery_actions": len(recover["actions"]),
            "semantic_equal_to_baseline": (
                resource_status["semantic_digest"]
                == baseline_status["semantic_digest"]
            ),
            "outcomes_equal_to_baseline": (
                resource_status["request_outcomes"]
                == baseline_status["request_outcomes"]
            ),
            "resources_equal_to_baseline": (
                resource_status["resources"] == baseline_status["resources"]
            ),
            "verify_ok": resource_verify["ok"],
        }

        dependency_crash_result, dependency_rc = run(
            "schedule",
            "--workdir",
            str(dependency_crash),
            "--round-budget-ms",
            "250",
            "--crash-at",
            "dependency",
            check=False,
        )
        if dependency_rc != 79:
            raise RuntimeError(
                f"dependency crash returned {dependency_rc}, expected 79"
            )
        run(
            "recover",
            "--workdir",
            str(dependency_crash),
            "--round-budget-ms",
            "250",
        )
        dependency_status = status(dependency_crash)
        report["scenarios"]["dependency_crash_recovery"] = {
            "crash_exit": dependency_rc,
            "semantic_equal_to_baseline": (
                dependency_status["semantic_digest"]
                == baseline_status["semantic_digest"]
            ),
            "verify_ok": verify(dependency_crash)["ok"],
        }

        policy_crash_result, policy_crash_rc = run(
            "build",
            "--workdir",
            str(policy_crash),
            "--policy-package",
            str(policy_crash / "samples" / "policies" / "v2-update"),
            "--crash-at",
            "policy-stage",
            check=False,
        )
        if policy_crash_rc != 78:
            raise RuntimeError(
                f"policy crash returned {policy_crash_rc}, expected 78"
            )
        policy_recover, _ = run(
            "recover",
            "--workdir",
            str(policy_crash),
            "--round-budget-ms",
            "250",
        )
        policy_status = status(policy_crash)
        report["scenarios"]["policy_crash_recovery"] = {
            "crash_exit": policy_crash_rc,
            "pending_jobs_replayed": len(policy_recover["policy_recovery"]),
            "semantic_equal_to_clean_update": (
                policy_status["semantic_digest"]
                == updated_status["semantic_digest"]
            ),
            "invalidations_equal_to_clean_update": (
                policy_status["invalidations"]
                == updated_status["invalidations"]
            ),
            "verify_ok": verify(policy_crash)["ok"],
        }

        report["ok"] = all(
            item.get("verify_ok", True)
            for item in report["scenarios"].values()
        ) and all(
            item.get("semantic_equal_to_baseline", True)
            for item in report["scenarios"].values()
        ) and all(
            item.get("semantic_equal_to_clean_update", True)
            for item in report["scenarios"].values()
        )
        report["workdirs"] = {
            "baseline": str(baseline),
            "updated": str(updated),
            "resource_crash": str(resource_crash),
            "policy_crash": str(policy_crash),
            "dependency_crash": str(dependency_crash),
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
            report["root"] = "(removed after run)"


if __name__ == "__main__":
    raise SystemExit(main())
