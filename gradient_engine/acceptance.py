from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .engine import verify_replay
from .generator import generate_samples
from .storage import Store


ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args)}\n{proc.stderr}\n{proc.stdout}")
    return proc


def _status(path: Path) -> dict[str, Any]:
    store = Store.connect(path)
    try:
        return store.status()
    finally:
        store.close()


def run_demo(workspace: Path | str = "demo_workspace", *, seed: int = 417) -> dict[str, Any]:
    root = Path(workspace)
    sample_dir = root / "samples"
    fresh_db = root / "fresh.db"
    crash_db = root / "crash_recovery.db"
    generate_samples(sample_dir, seed=seed)

    _run_cli("load", str(sample_dir), "--db", str(fresh_db))
    _run_cli("run", "--db", str(fresh_db))

    _run_cli("load", str(sample_dir), "--db", str(crash_db))
    crashed = _run_cli(
        "run",
        "--db",
        str(crash_db),
        "--crash-before-commit-round",
        "3",
        check=False,
    )
    if crashed.returncode != 86:
        raise RuntimeError(f"expected failpoint exit 86, got {crashed.returncode}")

    before = _status(crash_db)
    if before["open_rounds"] != [3]:
        raise RuntimeError(f"expected half-committed round 3, got {before['open_rounds']}")

    _run_cli("resume", "--db", str(crash_db))

    fresh_store = Store.connect(fresh_db)
    crash_store = Store.connect(crash_db)
    try:
        verification = verify_replay(crash_store)
        fresh_trace = fresh_store.canonical_trace()
        recovered_trace = crash_store.canonical_trace()
        fresh_status = fresh_store.status()
        recovered_status = crash_store.status()
    finally:
        fresh_store.close()
        crash_store.close()

    comparable_keys = [
        "active_spec_version",
        "clock_watermark_ms",
        "completion_sets",
        "round_count",
    ]
    status_match = all(fresh_status[key] == recovered_status[key] for key in comparable_keys)
    node_match = True
    for node_id, node_status in fresh_status["nodes"].items():
        recovered_node = recovered_status["nodes"][node_id]
        for key in ("held_shards", "completed_shards", "completed_shards_by_spec", "memory_bytes", "outcome_codes"):
            if node_status[key] != recovered_node[key]:
                node_match = False

    trace_match = fresh_trace == recovered_trace
    ok = verification["ok"] and status_match and node_match and trace_match
    return {
        "ok": ok,
        "workspace": str(root),
        "seed": seed,
        "crash_exit_code": crashed.returncode,
        "half_committed_rounds_before_resume": before["open_rounds"],
        "verification": verification,
        "trace_match": trace_match,
        "status_match": status_match,
        "node_ledger_match": node_match,
        "fresh_rounds": len(fresh_trace),
        "recovered_rounds": len(recovered_trace),
        "sample_dir": str(sample_dir),
        "databases": {"fresh": str(fresh_db), "crash_recovery": str(crash_db)},
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2, sort_keys=True))
