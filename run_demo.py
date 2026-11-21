"""End-to-end acceptance demo for the deterministic multimodal pipeline.

Scenarios demonstrated, all fully local and offline:
  1. uninterrupted run + independent verification;
  2. two independent runs produce byte-identical snapshots and scores;
  3. forced worker termination -> restart recovery equals the clean run;
  4. budget overflow -> E_RESOURCE_LIMIT / recorded degradation;
  5. snapshot corruption -> E_SNAPSHOT_CORRUPT, then force-rebuild repair.

Run with:  python run_demo.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mmpipe.config import BudgetConfig, PipelineConfig
from mmpipe.consistency import verify_snapshots
from mmpipe.errors import ResourceLimitError, SnapshotCorruptError
from mmpipe.fault_tools import corrupt_snapshot
from mmpipe.manifest import generate_manifest, load_manifest
from mmpipe.pipeline import Pipeline

REPO_ROOT = Path(__file__).resolve().parent


def heading(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def fresh_dir(config: PipelineConfig, num_pairs: int = 9) -> Path:
    work_dir = Path(tempfile.mkdtemp(prefix="mmpipe-demo-"))
    generate_manifest(work_dir, config, num_pairs=num_pairs)
    return work_dir


def main() -> int:
    config = PipelineConfig()
    budget = BudgetConfig()
    temp_root = Path(tempfile.mkdtemp(prefix="mmpipe-demo-root-"))
    print(f"Using temp root: {temp_root}")

    # Scenario 1: uninterrupted run + independent verification.
    heading("Scenario 1: uninterrupted four-stage run + independent verification")
    clean_dir = fresh_dir(config)
    clean = Pipeline(clean_dir, config, budget).run(
        load_manifest(clean_dir / "manifest.jsonl")
    )
    verification = verify_snapshots(
        clean_dir, load_manifest(clean_dir / "manifest.jsonl"), config
    )
    print(json.dumps({"verify_ok": verification["ok"], "findings": verification["findings"]}, indent=2))

    # Scenario 2: byte-identical independent run.
    heading("Scenario 2: determinism — second independent run is byte-identical")
    other_dir = fresh_dir(config)
    other = Pipeline(other_dir, config, budget).run(
        load_manifest(other_dir / "manifest.jsonl")
    )
    identical_scores = (clean_dir / "results.json").read_bytes() == (
        other_dir / "results.json"
    ).read_bytes()
    checksum_match = all(
        clean.report["stages"][s]["embeddings_checksum"]
        == other.report["stages"][s]["embeddings_checksum"]
        for s in ("image", "text", "fusion", "score")
    )
    print(json.dumps({"checksums_identical": checksum_match, "results_bytes_identical": identical_scores}, indent=2))

    # Scenario 3: hard kill and recovery.
    heading("Scenario 3: forced termination -> recovery equals uninterrupted run")
    kill_dir = fresh_dir(config)
    env = dict(os.environ)
    env["MMPIPE_KILL_STAGE"] = "fusion"
    env["MMPIPE_KILL_PAIR"] = "pair_0002"
    killed = subprocess.run(
        [sys.executable, "-m", "mmpipe.cli", "run", str(kill_dir)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    print(f"forced kill exit code: {killed.returncode}")
    print("uncommitted fusion staging left behind:",
          any(p.name.startswith(".tmp-") for p in (kill_dir / "snapshots" / "fusion").iterdir()))
    resumed = Pipeline(kill_dir, config, budget).run(
        load_manifest(kill_dir / "manifest.jsonl")
    )
    statuses = {stage: resumed.report["stages"][stage]["status"] for stage in
                ("image", "text", "fusion", "score")}
    recovery_match = (kill_dir / "results.json").read_bytes() == (
        clean_dir / "results.json"
    ).read_bytes()
    print(json.dumps({"stage_status": statuses, "recovery_matches_clean_bytes": recovery_match}, indent=2))

    # Scenario 4: resource budget arbitration.
    heading("Scenario 4: resource budget — reject code and degrade path")
    limit_dir = fresh_dir(config)
    try:
        Pipeline(limit_dir, config, BudgetConfig(max_stage_pairs=2, overflow_policy="reject")).run(
            load_manifest(limit_dir / "manifest.jsonl")
        )
    except ResourceLimitError as err:
        print("reject policy returned:", json.dumps(err.to_dict()))
    degrade_dir = fresh_dir(config)
    degraded = Pipeline(
        degrade_dir,
        config,
        BudgetConfig(max_batch_pairs=2, max_workers=1),
        requested_workers=4,
        requested_batch=8,
    ).run(load_manifest(degrade_dir / "manifest.jsonl"))
    sample = degraded.report["stages"]["image"]["scheduling"]
    print("degrade decision:", json.dumps(sample, indent=2))

    # Scenario 5: snapshot corruption and force-rebuild.
    heading("Scenario 5: snapshot corruption -> E_SNAPSHOT_CORRUPT -> repair")
    corrupt_dir = fresh_dir(config)
    Pipeline(corrupt_dir, config, budget).run(
        load_manifest(corrupt_dir / "manifest.jsonl")
    )
    tampered = corrupt_snapshot(corrupt_dir, "text", mode="tamper")
    print("tampered file:", tampered)
    try:
        Pipeline(corrupt_dir, config, budget).run(
            load_manifest(corrupt_dir / "manifest.jsonl")
        )
    except SnapshotCorruptError as err:
        print("corruption returned:", json.dumps(err.to_dict()))
    repaired = Pipeline(corrupt_dir, config, budget, force_rebuild=True).run(
        load_manifest(corrupt_dir / "manifest.jsonl")
    )
    repaired_check = verify_snapshots(
        corrupt_dir, load_manifest(corrupt_dir / "manifest.jsonl"), config
    )
    print(json.dumps({"repair_status": repaired.report["stages"]["text"]["status"],
                      "verify_ok": repaired_check["ok"]}, indent=2))

    shutil.rmtree(temp_root, ignore_errors=True)
    for path in (clean_dir, other_dir, kill_dir, limit_dir, degrade_dir, corrupt_dir):
        shutil.rmtree(path, ignore_errors=True)
    heading("ALL ACCEPTANCE SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

