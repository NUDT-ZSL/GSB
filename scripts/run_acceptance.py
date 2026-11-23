#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diagnostic_workbench.engine import Workbench
from diagnostic_workbench.util import canonical_json_text


def _worker(
    module: str,
    state_dir: Path,
    round_id: int,
    clock_ms: int,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(ROOT)
    if env:
        process_env.update(env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            module,
            str(state_dir),
            str(round_id),
            str(clock_ms),
        ],
        cwd=ROOT,
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )


def main() -> int:
    sample = json.loads((ROOT / "samples" / "sample.json").read_text("utf-8"))
    report: dict[str, object] = {
        "sample_digest": None,
        "flow": [],
        "query_isolation": {},
        "idempotency": {},
        "crash_recovery": {},
        "multi_process": {},
    }
    with tempfile.TemporaryDirectory(prefix="workbench-acceptance-") as temp:
        root = Path(temp)

        workbench = Workbench(root / "main-state")
        loaded = workbench.load({"sample_path": str(ROOT / "samples")})
        report["sample_digest"] = loaded["sample_digest"]
        pinned = workbench.storage.current_snapshot()
        assert pinned is not None
        old_query = workbench.query(
            "auth",
            pointer_version=pinned.pointer_version,
        )
        current_after_round_one: dict[str, object] | None = None

        for round_id, clock_ms in (
            (1, 130),
            (2, 900),
            (3, 1300),
            (4, 1700),
            (5, 2100),
            (6, 2700),
        ):
            result = workbench.switch(
                {"round_id": round_id, "logical_time_ms": clock_ms}
            )
            report["flow"].append(
                {
                    "domain_statuses": {
                        item["domain"]: item["status"]
                        for item in result["domains"]
                    },
                    "failback_steps": len(result["failback_steps"]),
                    "frozen_domains": result["frozen_domains"],
                    "pointer_version": result["pointer_version"],
                    "round_id": round_id,
                    "switches": len(result["switches"]),
                }
            )
            if round_id == 1:
                current_after_round_one = workbench.query("auth")

        new_query = workbench.query("auth", pointer_version=pinned.pointer_version)
        assert current_after_round_one is not None
        report["query_isolation"] = {
            "current_primary_after_round_one": current_after_round_one["primary"],
            "pinned_primary": new_query["primary"],
            "pinned_query_unchanged": new_query["query"] == old_query["query"],
        }

        round_one = workbench.switch(
            {"round_id": 1, "logical_time_ms": 130}
        )
        repeated = workbench.switch(
            {"round_id": 1, "logical_time_ms": 130}
        )
        report["idempotency"] = {
            "ledger_count": workbench.ledger(1)["count"],
            "response_identical": (
                canonical_json_text(round_one) == canonical_json_text(repeated)
            ),
        }
        verify = workbench.verify({"round_id": 1})
        report["round_1_verify"] = {
            "consistent_services": sum(
                1 for item in verify["services"] if item["equal"]
            ),
            "query_digests": {
                item["service"]: {
                    "after": item["after"]["query_digest"],
                    "before": item["before"]["query_digest"],
                }
                for item in verify["services"]
            },
            "verified": verify["verified"],
        }

        crash_state = root / "crash-state"
        crash_workbench = Workbench(crash_state)
        crash_workbench.load({"sample": copy.deepcopy(sample)})
        crashed = _worker(
            "tests.crash_worker",
            crash_state,
            1,
            130,
            env={"WORKBENCH_CRASH_AFTER_DOMAIN_COMMITS": "1"},
        )
        interrupted = Workbench(crash_state)
        before_resume = interrupted.status()
        resumed = interrupted.resume()
        report["crash_recovery"] = {
            "completed_domains_after_kill": len(
                before_resume["prepared_rounds"][0]["completed_domains"]
            ),
            "exit_code": crashed.returncode,
            "ledger_count_after_resume": interrupted.ledger(1)["count"],
            "resume_status": resumed["status"],
            "round_committed": (
                interrupted.status()["round_watermark"] == 1
            ),
        }

        concurrent_state = root / "concurrent-state"
        concurrent = Workbench(concurrent_state)
        concurrent.load({"sample": copy.deepcopy(sample)})
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tests.concurrency_worker",
                    str(concurrent_state),
                    "1",
                    "130",
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        outputs: list[str] = []
        return_codes: list[int] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=90)
            outputs.append(stdout)
            return_codes.append(process.returncode)
            if process.returncode != 0:
                print(stderr, file=sys.stderr)
        report["multi_process"] = {
            "distinct_responses": len(set(outputs)),
            "ledger_count": concurrent.ledger(1)["count"],
            "return_codes": return_codes,
        }

    output_path = ROOT / "artifacts" / "diagnostic_verification.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    assertions = [
        report["query_isolation"]["pinned_query_unchanged"],
        report["idempotency"]["response_identical"],
        report["idempotency"]["ledger_count"] == 4,
        report["round_1_verify"]["verified"],
        report["crash_recovery"]["exit_code"] == 77,
        report["crash_recovery"]["ledger_count_after_resume"] == 4,
        report["crash_recovery"]["round_committed"],
        report["multi_process"]["distinct_responses"] == 1,
        report["multi_process"]["ledger_count"] == 4,
        all(code == 0 for code in report["multi_process"]["return_codes"]),
    ]
    return 0 if all(assertions) else 1


if __name__ == "__main__":
    raise SystemExit(main())

