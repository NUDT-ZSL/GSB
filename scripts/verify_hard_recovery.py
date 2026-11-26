#!/usr/bin/env python3
"""Spawn a child that SIGKILLs itself after an uncommitted batch, then recover."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seg_diag.config_io import read_v1_config, read_v2_config
from seg_diag.hashing import canonical_bytes
from seg_diag.pipeline import _json_default, write_report
from seg_diag.recovery import RecoveryRunner
from seg_diag.sample_generator import DEFAULT_SEED, generate_sample_directory, load_sample_arrays
from seg_diag.selfcheck import run_self_check


SEED = DEFAULT_SEED
SAMPLE_DIR = ROOT / "samples" / f"local_seed_{SEED}"
STATE_DIR = ROOT / "runtime" / "hard_recovery"


def _load():
    generate_sample_directory(SAMPLE_DIR, seed=SEED)
    arrays = load_sample_arrays(SAMPLE_DIR)
    v1 = read_v1_config(SAMPLE_DIR / "config_v1.json")
    v2 = read_v2_config(SAMPLE_DIR / "config_v2")
    return arrays, v1, v2


def child_main() -> int:
    arrays, v1, v2 = _load()
    runner = RecoveryRunner(STATE_DIR, arrays, v1, v2, seed=SEED)

    def kill_self() -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    runner.run(batch_size=2, fail_in_batch_index=1, termination_callback=kill_self)
    return 0


def parent_main() -> int:
    if STATE_DIR.exists():
        for child in sorted(STATE_DIR.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        if STATE_DIR.exists():
            STATE_DIR.rmdir()

    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    killed_by_signal = child.returncode == -int(signal.SIGKILL)
    if not killed_by_signal:
        raise RuntimeError(
            f"child was not SIGKILLed: code={child.returncode}, stderr={child.stderr}"
        )

    arrays, v1, v2 = _load()
    killed_runner = RecoveryRunner(STATE_DIR, arrays, v1, v2, seed=SEED)
    killed_checkpoint = killed_runner.load_checkpoint()
    committed_before = killed_checkpoint["completed_regions"] if killed_checkpoint else []

    recovered = RecoveryRunner(STATE_DIR, arrays, v1, v2, seed=SEED).run(batch_size=2)
    idle = RecoveryRunner(STATE_DIR, arrays, v1, v2, seed=SEED).run(batch_size=2)
    self_check = run_self_check(
        arrays=arrays,
        v1_config=v1,
        v2_config=v2,
        ledger=recovered["ledger"],
        seed=SEED,
        checkpoint=recovered["checkpoint"],
        expected_region_count=16,
    )
    report = {
        "business_code": "E_OK",
        "child_returncode": child.returncode,
        "child_killed_by_sigkill": killed_by_signal,
        "committed_regions_after_kill": committed_before,
        "recovery_computed_regions": recovered["computed_regions"],
        "recovery_reused_regions": recovered["reused_committed_regions"],
        "committed_region_recompute_disjoint": set(recovered["computed_regions"]).isdisjoint(
            committed_before
        ),
        "idle_restart_computed_regions": idle["computed_regions"],
        "checkpoint": recovered["checkpoint"],
        "self_check": self_check,
    }
    write_report(ROOT / "reports" / "hard_recovery_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    if "--child" in sys.argv:
        raise SystemExit(child_main())
    raise SystemExit(parent_main())
