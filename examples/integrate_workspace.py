"""Library integration example.

Run from the project root:
    python examples/integrate_workspace.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sandbox_guard.parsing import parse_policy
from sandbox_guard.samplegen import generate_sample
from sandbox_guard.workspace import Workspace


def main() -> int:
    sample_root = PROJECT_ROOT / "samples" / "generated"
    if not (sample_root / "policy.json").exists():
        generate_sample(sample_root, seed=361)

    policy = parse_policy(
        json.loads((sample_root / "policy.json").read_text(encoding="utf-8"))
    )
    work_root = Path(tempfile.mkdtemp(prefix="sandbox-guard-library-"))
    workspace = Workspace(work_root / "ledger")
    workspace.initialize_policy(policy)

    for patch_path in sorted((sample_root / "patches").glob("*.json")):
        result = workspace.import_batch_file(patch_path)
        print(patch_path.name, result["status"])

    process = workspace.query_process("4103")
    closure = workspace.closure_report()
    verify = workspace.verify_checkpoint()

    output = {
        "workspace": str(work_root / "ledger"),
        "process_4103": process["processes"][0],
        "closure_grant_keys": closure["closure_grant_keys"],
        "affected_pids": closure["affected_pids"],
        "verify": {
            "ok": verify["ok"],
            "checkpoint_id": verify.get("checkpoint_id"),
            "ledger_digest": verify.get("ledger_digest"),
        },
    }
    report_path = PROJECT_ROOT / "artifacts" / "reports" / "library-integration.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(output, indent=2, sort_keys=True))
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
