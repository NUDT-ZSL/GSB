from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from rope_attention.demo import run_demo, run_fault_demo
from rope_attention.process_demo import (
    run_kill_restart_demo,
    run_process_pool_demo,
)
from rope_attention.util import canonical_json_bytes


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _max_deviations(demo: dict[str, Any]) -> dict[str, float]:
    output_abs = 0.0
    output_rel = 0.0
    denominator_abs = 0.0
    denominator_rel = 0.0
    for run in demo["version_runs"].values():
        for case in run["cases"].values():
            comparison = case["block_deviation"]
            output_abs = max(output_abs, comparison["max_abs_output_deviation"])
            output_rel = max(output_rel, comparison["max_rel_output_deviation"])
            denominator_abs = max(
                denominator_abs,
                comparison["max_abs_denominator_deviation"],
            )
            denominator_rel = max(
                denominator_rel,
                comparison["max_rel_denominator_deviation"],
            )
    return {
        "max_abs_denominator_deviation": denominator_abs,
        "max_abs_output_deviation": output_abs,
        "max_rel_denominator_deviation": denominator_rel,
        "max_rel_output_deviation": output_rel,
    }


def main() -> int:
    reports = ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    pytest = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    (reports / "pytest.txt").write_text(
        pytest.stdout + pytest.stderr,
        encoding="utf-8",
    )

    demo_first = run_demo(ROOT)
    demo_second = run_demo(ROOT)
    demo_reproducible = canonical_json_bytes(demo_first) == canonical_json_bytes(
        demo_second
    )
    _write(reports / "demo.json", demo_second)

    fault = run_fault_demo(ROOT)
    _write(reports / "fault_demo.json", fault)
    process_pool = run_process_pool_demo(ROOT, workers=4)
    _write(reports / "process_pool.json", process_pool)
    kill_restart = run_kill_restart_demo(ROOT, total_entries=40)
    _write(reports / "kill_restart.json", kill_restart)

    stages = {
        stage["label"]: {
            "byte_equal_to_full_recompute": stage["byte_equal_to_full_recompute"][
                "all_byte_identical"
            ],
            "recomputed_node_count": len(
                stage["incremental"]["recomputed_nodes"]
            ),
            "reused_node_count": len(stage["incremental"]["reused_nodes"]),
        }
        for stage in demo_second["incremental"]["stages"]
    }
    pytest_lines = [line for line in pytest.stdout.splitlines() if line.strip()]
    pytest_summary = next(
        (line for line in reversed(pytest_lines) if "passed" in line),
        pytest_lines[-1] if pytest_lines else "",
    )
    summary = {
        "business_codes": {
            "demo": demo_second["business_codes"],
            "fault_demo": fault["business_codes"],
            "kill_restart": kill_restart["business_code"],
            "process_pool": process_pool["business_code"],
        },
        "demo_repeat_byte_identical": demo_reproducible,
        "environment": {
            "numpy": np.__version__,
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "fault_demo_ok": fault["ok"],
        "incremental_stages": stages,
        "kill_restart": {
            "entries_after_force_kill": kill_restart[
                "entries_after_force_kill"
            ],
            "expected_entry_count": kill_restart["expected_entry_count"],
            "ok": kill_restart["ok"],
            "verified_after_restart": kill_restart[
                "verification_after_restart"
            ]["checked_entries"],
        },
        "max_block_vs_full_deviation": _max_deviations(demo_second),
        "ok": (
            pytest.returncode == 0
            and demo_second["ok"]
            and demo_reproducible
            and fault["ok"]
            and process_pool["ok"]
            and kill_restart["ok"]
        ),
        "process_pool": {
            "exactly_one_effective_writer": process_pool[
                "exactly_one_effective_writer"
            ],
            "ok": process_pool["ok"],
            "statuses": sorted(item["status"] for item in process_pool["writes"]),
        },
        "pytest_returncode": pytest.returncode,
        "pytest_summary": pytest_summary.strip(),
        "sample_manifest_checksum": demo_second["sample_manifest_checksum"],
        "schema": "rope-attention-validation-summary/v1",
    }
    _write(reports / "validation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

