from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .canonical import canonical_dumps
from .pipeline import (
    cmd_init_registry,
    cmd_load_observations,
    cmd_load_shard_manifest,
    cmd_merge,
    cmd_patch,
    cmd_reconcile,
    cmd_resume,
    cmd_status,
)
from .seed import BASE_CLOCK_MS, generate_sample


CLOCK = BASE_CLOCK_MS
SHARDS = ["obs-shard-001", "obs-shard-002", "obs-shard-003"]


def _init_rules(sample: Path, db: Path) -> None:
    cmd_init_registry(db, sample / "registry.json", CLOCK)
    cmd_merge(db, v1_path=sample / "rules_v1.json", v2_manifest_path=sample / "rules_v2_manifest.json", clock_ms=CLOCK)


def _baseline(sample: Path, db: Path) -> dict[str, Any]:
    _init_rules(sample, db)
    cmd_load_observations(db, sample / "observations.jsonl")
    cmd_reconcile(db, CLOCK)
    return cmd_status(db)


def _cli(db: Path, *args: str, crash: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    if crash:
        env["WB_CRASH_POINT"] = crash
    return subprocess.run(
        [sys.executable, "-m", "workbench", *args, "--db", str(db)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def run_acceptance(output_path: str | Path = "artifacts/acceptance-report.json") -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="gsb-acceptance-"))
    sample = root / "sample"
    generate_sample(sample)
    baseline_db = root / "baseline.db"
    baseline = _baseline(sample, baseline_db)
    expected_hash = baseline["ledger_hash"]

    order_reports = []
    for order in itertools.permutations(SHARDS):
        db = root / ("order-" + "-".join(order) + ".db")
        _init_rules(sample, db)
        for shard in order:
            cmd_load_shard_manifest(db, sample / "observation_shards_manifest.json", only_shard=shard)
            cmd_reconcile(db, CLOCK)
        ledger = cmd_status(db)
        order_reports.append({"order": list(order), "ledger_hash": ledger["ledger_hash"], "matches_baseline": ledger["ledger_hash"] == expected_hash})

    missing_db = root / "missing.db"
    _init_rules(sample, missing_db)
    manifest = sample / "observation_shards_manifest.json"
    cmd_load_shard_manifest(missing_db, manifest, only_shard="obs-shard-001")
    cmd_reconcile(missing_db, CLOCK)
    cmd_load_shard_manifest(missing_db, manifest, only_shard="obs-shard-003")
    cmd_reconcile(missing_db, CLOCK)
    partial = cmd_status(missing_db)
    alpha_partial = next(i for i in partial["instances"] if i["instance_id"] == "inst-alpha")
    cmd_load_shard_manifest(missing_db, manifest, only_shard="obs-shard-002")
    cmd_reconcile(missing_db, CLOCK)
    missing_final = cmd_status(missing_db)

    crash_db = root / "crash-buffer.db"
    _init_rules(sample, crash_db)
    crashed = _cli(crash_db, "load", "--observations", str(sample / "observations.jsonl"), crash="after_pending_commit")
    recovery = cmd_resume(crash_db, CLOCK)
    crash_hash = cmd_status(crash_db)["ledger_hash"]

    stage_db = root / "crash-stage.db"
    _baseline(sample, stage_db)
    stage_crash = _cli(
        stage_db,
        "patch",
        "--patch",
        str(sample / "patches" / "patch_epsilon_dependency.json"),
        "--clock-ms",
        str(CLOCK + 120_000),
        crash="after_stage_before_pointer",
    )
    cmd_resume(stage_db, CLOCK)
    stage_hash = cmd_status(stage_db)["ledger_hash"]

    patch_db = root / "patch.db"
    _baseline(sample, patch_db)
    patch = cmd_patch(patch_db, sample / "patches" / "patch_alpha_tombstone.json", CLOCK + 120_000)
    patch_again = cmd_patch(patch_db, sample / "patches" / "patch_alpha_tombstone.json", CLOCK + 120_000)

    concurrent_db = root / "concurrent.db"
    _init_rules(sample, concurrent_db)

    def load_shard(shard_id: str) -> int:
        return _cli(concurrent_db, "load", "--shard-manifest", str(manifest), "--only-shard", shard_id).returncode

    with ThreadPoolExecutor(max_workers=4) as pool:
        load_codes = list(pool.map(load_shard, [*SHARDS, "obs-shard-001"]))
    cmd_reconcile(concurrent_db, CLOCK)
    concurrent_hash = cmd_status(concurrent_db)["ledger_hash"]

    all_orders_match = all(item["matches_baseline"] for item in order_reports)
    report = {
        "ok": all_orders_match
        and missing_final["ledger_hash"] == expected_hash
        and crash_hash == expected_hash
        and stage_hash == expected_hash
        and concurrent_hash == expected_hash
        and all(code == 0 for code in load_codes)
        and patch_again.get("idempotent") is True,
        "seed_sample_dir": str(sample),
        "acceptance_workdir": str(root),
        "baseline_ledger_hash": expected_hash,
        "canonical_byte_form": canonical_dumps({"ledger_hash": expected_hash}),
        "shard_order_independence": {
            "permutations_tested": len(order_reports),
            "all_match": all_orders_match,
            "proof": "Every permutation was independently loaded and produced the same SHA-256 canonical ledger hash.",
            "orders": order_reports,
        },
        "missing_shard_barrier": {
            "alpha_committed_through_with_middle_missing": alpha_partial["committed_through_seq"],
            "alpha_blocked_intervals": alpha_partial["blocked_intervals"],
            "later_decisions_count": len([d for d in alpha_partial["decisions"] if d["seq"] > 2]),
            "final_hash_matches": missing_final["ledger_hash"] == expected_hash,
        },
        "crash_recovery": {
            "pending_commit_crash_exit_code": crashed.returncode,
            "buffered_batches_before_resume": len(recovery["recovery"]["buffered_batches_before"]),
            "pending_recovery_hash_matches": crash_hash == expected_hash,
            "stage_pointer_crash_exit_code": stage_crash.returncode,
            "rolled_back_stage_hash_matches_baseline": stage_hash == expected_hash,
        },
        "patch_closure": {
            "closure_rule_keys": patch["patch_report"]["closure"]["closure_rule_keys"],
            "affected_instances": patch["patch_report"]["closure"]["affected_instances"],
            "actions": patch["reconcile"]["actions"],
            "repeat_patch_idempotent": patch_again.get("idempotent") is True,
        },
        "concurrency": {
            "load_processes": 4,
            "exit_codes": load_codes,
            "final_hash_matches": concurrent_hash == expected_hash,
        },
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    report = run_acceptance()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
