from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coldstart_ledger.engine import ColdStartEngine  # noqa: E402
from coldstart_ledger.loader import load_inputs  # noqa: E402


def main() -> int:
    bundle = load_inputs(ROOT)
    with tempfile.TemporaryDirectory(prefix="coldstart-acceptance-") as temporary:
        work = Path(temporary)

        engine_a = ColdStartEngine(work / "replay-a.db", bundle)
        round_a = engine_a.run_round("sample-round", 6).to_dict()
        audit_a = engine_a.audit()
        snapshot_a = engine_a.snapshot()

        engine_b = ColdStartEngine(work / "replay-b.db", bundle)
        engine_b.run_round("sample-round", 6)
        snapshot_b = engine_b.snapshot()
        recovered = engine_a.recover()

        preemption_engine = ColdStartEngine(work / "preemption.db", bundle)
        preemption_engine.admit("worker-low", "preempt-r1", 1)
        preemption_engine.admit("api-fast", "preempt-r2", 2)
        preview = preemption_engine.plan_preemption("api-batch", 3)
        applied = preemption_engine.admit("api-batch", "preempt-r3", 3)
        preemption_state = preemption_engine.snapshot()["state"]
        preemption_audit = preemption_engine.audit()

        pytest = subprocess.run(
            [sys.executable, "-m", "pytest", "-o", "addopts=", "-q"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        passed_match = re.search(r"(\d+) passed", pytest.stdout)
        tests_passed = int(passed_match.group(1)) if passed_match else pytest.stdout.count(".")
        verification = {
            "status": "PASS" if (
                audit_a["ok"]
                and preemption_audit["ok"]
                and snapshot_a["state_sha256"] == snapshot_b["state_sha256"]
                and recovered["state_sha256"] == snapshot_a["state_sha256"]
                and preview["can_admit_after"]
                and applied["stage"] == "READING_LAYERS"
                and pytest.returncode == 0
            ) else "FAIL",
            "runtime": {
                "python": sys.version.split()[0],
                "sqlite": sqlite3.sqlite_version,
            },
            "inputs": {
                "instances": len(bundle.instances),
                "layers": len(bundle.layers),
                "nodes": len(bundle.nodes),
            },
            "sample_round": {
                "round_id": round_a["round_id"],
                "logical_clock_ms": round_a["logical_clock_ms"],
                "summary": round_a["summary"],
                "audit_ok": audit_a["ok"],
                "global_charged_ephemeral_bytes": audit_a[
                    "global_charged_ephemeral_bytes"
                ],
                "state_sha256": snapshot_a["state_sha256"],
            },
            "bitwise_replay": {
                "equal": snapshot_a["state_sha256"] == snapshot_b["state_sha256"],
                "state_sha256": snapshot_b["state_sha256"],
            },
            "recovery": {
                "equal_after_reopen": recovered["state_sha256"]
                == snapshot_a["state_sha256"],
                "audit_ok": recovered["audit"]["ok"],
                "ledger_version": recovered["revision"]["version"],
            },
            "preemption": {
                "requesting_instance": "api-batch",
                "reclaim_chain": [
                    item["instance_id"] for item in preview["reclaim_chain"]
                ],
                "can_admit_after": preview["can_admit_after"],
                "freed_ephemeral_bytes": preview["freed_ephemeral_bytes"],
                "applied_stage": applied["stage"],
                "refunds": preemption_state["refunds"],
                "audit_ok": preemption_audit["ok"],
                "state_sha256": preemption_engine.snapshot()["state_sha256"],
            },
            "pytest": {
                "command": f"{Path(sys.executable).name} -m pytest -q",
                "returncode": pytest.returncode,
                "tests_passed": tests_passed,
                "result": pytest.stdout.strip(),
            },
        }

        output = ROOT / "artifacts" / "verification.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if verification["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

