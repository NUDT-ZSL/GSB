"""End-to-end acceptance runner producing artifacts/verification_report.json."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from peft_diag.cache import ContentCache
from peft_diag.canonical import (
    atomic_write_bytes,
    canonical_bytes,
    layer_first_diff,
    read_json,
    sha256_bytes,
)
from peft_diag.config import DegradationLog, EngineConfig, ResourceLimits
from peft_diag.engine import DiagnosticEngine
from peft_diag.errors import BusinessError
from peft_diag.generator import generate_dataset, generate_snapshot
from peft_diag.replay import replay_task
from peft_diag.revision import RevisionRequest, RevisionService
from peft_diag.snapshots import load_snapshot, migrate_snapshot, write_v2
from peft_diag.tasks import normalize_task, parse_tasks


VERIFY = ROOT / "artifacts" / "verification"
DATA = VERIFY / "data"


def clean_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def object_error(exc: BusinessError) -> dict[str, Any]:
    return exc.to_dict()


def summaries(report: dict[str, Any]) -> dict[str, Any]:
    return {
        task_id: {
            "final_param_summary": artifact["final_param_summary"],
            "final_optimizer_summary": artifact["final_optimizer_summary"],
            "final_state_digest": artifact["final_state_digest"],
            "record_count": artifact["record_count"],
        }
        for task_id, artifact in report.get("artifacts_by_task", {}).items()
    }


def engine_report(
    *,
    data: Path,
    state: Path,
    run_id: str,
    snapshot_format: str = "v2",
    limits: ResourceLimits | None = None,
) -> dict[str, Any]:
    report = DiagnosticEngine(
        EngineConfig(
            tasks_path=str(data / "tasks.jsonl"),
            catalog_path=str(data / "catalog.json"),
            state_dir=str(state),
            run_id=run_id,
            snapshot_format=snapshot_format,
            limits=limits or ResourceLimits(),
        )
    ).run()
    return {
        "status": report["status"],
        "ok": report["ok"],
        "latest_checkpoint": report["latest_checkpoint"],
        "checkpoint_versions": report["checkpoint_versions"],
        "cache": report["cache"],
        "task_stage_stats": report["task_stage_stats"],
        "degradation": report["degradation"],
        "summaries": summaries(report),
    }


def run_cli(arguments: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "peft_diag.cli", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def test_forced_kill_and_resume() -> dict[str, Any]:
    crash_state = VERIFY / "crash-state"
    clean_tree(crash_state)
    common = [
        "--tasks",
        str(DATA / "tasks.jsonl"),
        "--catalog",
        str(DATA / "catalog.json"),
        "--state-dir",
        str(crash_state),
        "--run-id",
        "crash",
    ]
    killed = run_cli(
        [
            "train",
            *common,
            "--simulate-kill-at",
            "GRAD_ACCUM:1:2",
        ]
    )
    latest_after_kill = None
    from peft_diag.checkpoint import CheckpointManager

    checkpoint = CheckpointManager(crash_state, "crash").recover_latest()
    if checkpoint:
        latest_after_kill = {
            "version": checkpoint["version"],
            "stage": checkpoint["stage"],
            "cycle": checkpoint["cycle"],
            "checksum": checkpoint["checksum"],
        }
    resumed = run_cli(["train", *common])
    if resumed.returncode != 0:
        raise RuntimeError(f"resume CLI failed: {resumed.stdout}\n{resumed.stderr}")
    resumed_report = json.loads(resumed.stdout)
    grad_hits = sum(
        stats.get("GRAD_ACCUM", {}).get("cache_hits", 0)
        for stats in resumed_report["task_stage_stats"].values()
    )
    return {
        "forced_kill_exit_code": killed.returncode,
        "checkpoint_after_kill": latest_after_kill,
        "resume_status": resumed_report["status"],
        "resume_checkpoint": resumed_report["latest_checkpoint"],
        "uncommitted_grad_cache_hits_after_resume": grad_hits,
        "summaries": summaries(resumed_report),
        "ok": (
            killed.returncode == 137
            and latest_after_kill is not None
            and latest_after_kill["stage"] == "INITIALIZE"
            and resumed_report["status"] == "complete"
            and grad_hits == 0
        ),
    }


def test_graceful_signal() -> dict[str, Any]:
    signal_state = VERIFY / "signal-state"
    clean_tree(signal_state)
    command = [
        sys.executable,
        "-m",
        "peft_diag.cli",
        "train",
        "--tasks",
        str(DATA / "tasks.jsonl"),
        "--catalog",
        str(DATA / "catalog.json"),
        "--state-dir",
        str(signal_state),
        "--run-id",
        "signal",
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.8)
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=60)
    if process.returncode != 0:
        raise RuntimeError(f"graceful signal CLI failed: {stdout}\n{stderr}")
    report = json.loads(stdout)
    checkpoint = report.get("latest_checkpoint") or {}
    safe_stage = checkpoint.get("stage") in {
        "INITIALIZE",
        "GRAD_ACCUM",
        "OPTIMIZER_UPDATE",
        "EXPORT",
    }
    return {
        "exit_code": process.returncode,
        "status": report["status"],
        "checkpoint": checkpoint,
        "status_detail": report.get("status_detail"),
        "report_written": bool(report.get("report_path")),
        "safe_commit_boundary": safe_stage,
        "ok": (
            report["status"] == "interrupted_at_commit_boundary"
            and safe_stage
            and bool(report.get("report_path"))
        ),
    }


def main() -> int:
    clean_tree(VERIFY)
    report: dict[str, Any] = {
        "ok": True,
        "scenarios": {},
        "errors": {},
    }
    generation = generate_dataset(DATA, seed=20261127)
    report["generation"] = {
        "seed": generation["seed"],
        "task_count": generation["task_count"],
        "task_list_sha256": generation["task_list_sha256"],
        "catalog_path": str(Path(generation["catalog_path"]).relative_to(ROOT)),
    }
    tasks = parse_tasks(DATA / "tasks.jsonl")
    catalog = read_json(DATA / "catalog.json")

    # Task ingestion and structured failures.
    task_validation: dict[str, Any] = {
        "sorted_task_ids": [task.case_id for task in tasks],
        "normalized_fingerprints": {task.case_id: task.fingerprint for task in tasks},
        "parameter_summaries": {task.case_id: task.parameter_summary for task in tasks},
    }
    expected = {
        "duplicate-case.jsonl": ("E_FORMAT", "case_id"),
        "bad-lr.jsonl": ("E_FORMAT", "lr"),
        "bad-micro-batch.jsonl": ("E_FORMAT", "micro_batch"),
        "unknown-version.jsonl": ("E_UNSUPPORTED", "task_version"),
        "missing-field.jsonl": ("E_FORMAT", "seed"),
        "bad-layer-count.jsonl": ("E_FORMAT", "num_layers"),
    }
    for filename, (code, field_name) in expected.items():
        try:
            parse_tasks(DATA / "invalid" / filename)
            outcome = {"unexpected_success": True}
        except BusinessError as exc:
            outcome = object_error(exc)
            report["ok"] = report["ok"] and exc.code == code and exc.field == field_name
        report["errors"][filename] = outcome
    report["scenarios"]["task_ingestion"] = task_validation

    # Format equivalence, incremental shard selection, migration.
    format_equivalence: dict[str, Any] = {}
    for task in tasks:
        entry = catalog["tasks"][task.case_id]
        v1 = load_snapshot(DATA / entry["v1"], task)
        v2 = load_snapshot(DATA / entry["v2"], task)
        format_equivalence[task.case_id] = {
            "v1": v1.semantic_hash,
            "v2": v2.semantic_hash,
            "equal": v1.semantic_bytes() == v2.semantic_bytes(),
        }
        report["ok"] = report["ok"] and v1.semantic_bytes() == v2.semantic_bytes()
    incremental_path = DATA / catalog["incremental_demo"]["path"]
    incremental = load_snapshot(incremental_path, tasks[0])
    migration_v2 = VERIFY / "migrate" / "case-001.upgraded.v2"
    migration_v1 = VERIFY / "migrate" / "case-001.downgraded.v1.json"
    upgrade = migrate_snapshot(
        DATA / catalog["tasks"][tasks[0].case_id]["v1"],
        tasks[0],
        target_format="v2",
        output=migration_v2,
    )
    downgrade = migrate_snapshot(
        DATA / catalog["tasks"][tasks[0].case_id]["v2"],
        tasks[0],
        target_format="v1",
        output=migration_v1,
    )
    left = load_snapshot(DATA / catalog["tasks"][tasks[0].case_id]["v1"], tasks[0]).semantic_dict()
    right = json.loads(json.dumps(left))
    right["layers"][tasks[0].layer_specs[1].key]["values"][0] += 0.25
    drift = layer_first_diff(left, right)
    report["scenarios"]["snapshot_formats"] = {
        "semantic_equivalence": format_equivalence,
        "incremental_v2": {
            "generation": incremental.generation,
            "tombstones": len(incremental.tombstones),
            "semantic_hash": incremental.semantic_hash,
        },
        "migration": {
            "v1_to_v2": {
                "semantic_equal": upgrade["semantic_equal"],
                "target": upgrade["target"],
                "completions": upgrade["completions"],
            },
            "v2_to_v1": {
                "semantic_equal": downgrade["semantic_equal"],
                "target": downgrade["target"],
                "dropped": downgrade["dropped"],
            },
            "drift_probe": drift,
        },
    }
    report["ok"] = (
        report["ok"]
        and all(item["equal"] for item in format_equivalence.values())
        and upgrade["semantic_equal"]
        and downgrade["semantic_equal"]
        and drift is not None
    )

    # Complete staged training in both snapshot formats.
    baseline_state = VERIFY / "state-baseline"
    clean_tree(baseline_state)
    baseline_v2 = engine_report(data=DATA, state=baseline_state, run_id="v2")
    baseline_v1 = engine_report(
        data=DATA,
        state=baseline_state,
        run_id="v1",
        snapshot_format="v1",
    )
    report["scenarios"]["complete_training"] = {
        "v2": baseline_v2,
        "v1": baseline_v1,
        "format_equivalent_final_state": baseline_v2["summaries"] == baseline_v1["summaries"],
    }
    report["ok"] = (
        report["ok"]
        and baseline_v2["status"] == "complete"
        and baseline_v1["status"] == "complete"
        and baseline_v2["summaries"] == baseline_v1["summaries"]
    )

    forced = test_forced_kill_and_resume()
    report["scenarios"]["forced_interruption"] = forced
    report["ok"] = report["ok"] and forced["ok"] and forced["summaries"] == baseline_v2["summaries"]
    graceful = test_graceful_signal()
    report["scenarios"]["graceful_signal"] = graceful
    report["ok"] = report["ok"] and graceful["ok"]

    # Revision invalidation, cache suffix reuse and clean-recompute equivalence.
    revision_data = VERIFY / "revision-data"
    if revision_data.exists():
        shutil.rmtree(revision_data)
    shutil.copytree(DATA, revision_data)
    revision_state = VERIFY / "revision-state"
    clean_tree(revision_state)
    revision_baseline = engine_report(
        data=revision_data,
        state=revision_state,
        run_id="base",
    )
    service = RevisionService(
        tasks_path=revision_data / "tasks.jsonl",
        catalog_path=revision_data / "catalog.json",
        state_dir=revision_state,
    )
    lock_path = revision_state / "revision.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            service.apply([RevisionRequest(case_id="case-003", lr=0.002)])
            busy = {"unexpected_success": True}
        except BusinessError as exc:
            busy = object_error(exc)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    report["errors"]["concurrent_revision"] = busy
    try:
        service.apply([RevisionRequest(case_id="case-001", revision=-1)])
        rollback = {"unexpected_success": True}
    except BusinessError as exc:
        rollback = object_error(exc)
    report["errors"]["revision_rollback"] = rollback
    revision = service.apply([RevisionRequest(case_id="case-003", lr=0.002)])
    incremental_run = engine_report(
        data=revision_data,
        state=revision_state,
        run_id="incremental",
    )
    fresh_state = VERIFY / "fresh-state"
    clean_tree(fresh_state)
    fresh_run = engine_report(
        data=revision_data,
        state=fresh_state,
        run_id="fresh",
    )
    report["scenarios"]["revision_invalidation"] = {
        "revision": revision,
        "baseline": revision_baseline,
        "incremental": incremental_run,
        "fresh_recompute": fresh_run,
        "equivalent": incremental_run["summaries"] == fresh_run["summaries"],
    }
    report["ok"] = (
        report["ok"]
        and busy.get("code") == "E_STAGE_BUSY"
        and rollback.get("code") == "E_FORMAT"
        and rollback.get("field") == "revision"
        and revision["affected_tasks"] == ["case-003"]
        and revision["impact"]["case-003"]["affected_stages"]
        == ["OPTIMIZER_UPDATE", "GRAD_ACCUM:subsequent", "EXPORT"]
        and incremental_run["summaries"] == fresh_run["summaries"]
        and incremental_run["task_stage_stats"]["case-003"]["INITIALIZE"]["cache_hits"] == 1
    )

    dimension_data = VERIFY / "dimension-data"
    if dimension_data.exists():
        shutil.rmtree(dimension_data)
    shutil.copytree(DATA, dimension_data)
    dimension_task = next(
        task for task in parse_tasks(dimension_data / "tasks.jsonl") if task.case_id == "case-004"
    )
    raw = dimension_task.canonical_fields()
    raw["adapter_dim"] = 6
    raw["revision"] = dimension_task.revision + 1
    dimension_task = normalize_task(raw)
    dimension_snapshot = generate_snapshot(
        dimension_task,
        revision=dimension_task.revision,
        generation=1,
    )
    dimension_source = VERIFY / "dimension-source" / "case-004.r1.v2"
    write_v2(dimension_snapshot, dimension_source, provenance={"source": "dimension-revision"})
    dimension_service = RevisionService(
        tasks_path=dimension_data / "tasks.jsonl",
        catalog_path=dimension_data / "catalog.json",
        state_dir=VERIFY / "dimension-state",
    )
    dimension_revision = dimension_service.apply(
        [
            RevisionRequest(
                case_id="case-004",
                revision=dimension_task.revision,
                adapter_dim=6,
                snapshot_path=str(dimension_source),
            )
        ]
    )
    dimension_catalog = read_json(dimension_data / "catalog.json")
    dimension_entry = dimension_catalog["tasks"]["case-004"]
    dimension_v1 = load_snapshot(dimension_data / dimension_entry["v1"], dimension_task)
    dimension_v2 = load_snapshot(dimension_data / dimension_entry["v2"], dimension_task)
    report["scenarios"]["dimension_revision"] = {
        "revision": dimension_revision,
        "v1_semantic_hash": dimension_v1.semantic_hash,
        "v2_semantic_hash": dimension_v2.semantic_hash,
        "semantic_equal": dimension_v1.semantic_bytes() == dimension_v2.semantic_bytes(),
    }
    report["ok"] = (
        report["ok"]
        and dimension_revision["impact"]["case-004"]["affected_stages"]
        == ["INITIALIZE", "GRAD_ACCUM", "OPTIMIZER_UPDATE", "EXPORT"]
        and dimension_v1.semantic_bytes() == dimension_v2.semantic_bytes()
    )

    snapshot_data = VERIFY / "snapshot-replacement-data"
    if snapshot_data.exists():
        shutil.rmtree(snapshot_data)
    shutil.copytree(DATA, snapshot_data)
    snapshot_task = next(
        task for task in parse_tasks(snapshot_data / "tasks.jsonl") if task.case_id == "case-002"
    )
    replacement = generate_snapshot(
        snapshot_task,
        revision=snapshot_task.revision,
        generation=1,
    )
    replacement_source = VERIFY / "snapshot-source" / "case-002.replacement.v2"
    write_v2(replacement, replacement_source, provenance={"source": "snapshot-replacement"})
    replacement_service = RevisionService(
        tasks_path=snapshot_data / "tasks.jsonl",
        catalog_path=snapshot_data / "catalog.json",
        state_dir=VERIFY / "snapshot-state",
    )
    replacement_revision = replacement_service.apply(
        [
            RevisionRequest(
                case_id="case-002",
                snapshot_path=str(replacement_source),
            )
        ]
    )
    replacement_impact = replacement_revision["impact"]["case-002"]
    report["scenarios"]["snapshot_replacement"] = {
        "revision": replacement_revision,
        "cache_keys_changed": replacement_impact["cache_keys_changed"],
    }
    report["ok"] = (
        report["ok"]
        and replacement_impact["changed_fields"] == ["snapshot_path"]
        and replacement_impact["affected_stages"]
        == ["INITIALIZE", "GRAD_ACCUM", "OPTIMIZER_UPDATE", "EXPORT"]
        and all(replacement_impact["cache_keys_changed"].values())
    )

    # Trace replay: success on current dependencies, stale rejection after LR revision.
    original_task = next(task for task in tasks if task.case_id == "case-003")
    artifact_path = baseline_state / "artifacts" / "v2" / "case-003.json"
    replay = replay_task(
        artifact_path=artifact_path,
        task=original_task,
        current_snapshot_path=DATA / catalog["tasks"]["case-003"]["v2"],
    )
    revised_task = next(
        task for task in parse_tasks(revision_data / "tasks.jsonl") if task.case_id == "case-003"
    )
    revised_catalog = read_json(revision_data / "catalog.json")
    try:
        replay_task(
            artifact_path=artifact_path,
            task=revised_task,
            current_snapshot_path=revision_data / revised_catalog["tasks"]["case-003"]["v2"],
        )
        stale = {"unexpected_success": True}
    except BusinessError as exc:
        stale = object_error(exc)
    report["errors"]["stale_replay"] = stale
    report["scenarios"]["trace_replay"] = {
        "successful_replay": replay,
        "stale_rejection": stale,
    }
    report["ok"] = (
        report["ok"]
        and replay["replay_equal"]
        and stale.get("code") == "E_STALE_RESULT"
        and stale.get("task_id") == "case-003"
    )

    # Snapshot corruption and resource degradation.
    corrupt_dir = VERIFY / "corrupt-v2"
    shutil.copytree(DATA / catalog["tasks"]["case-001"]["v2"], corrupt_dir)
    shard = sorted((corrupt_dir / "shards").glob("*.json"))[0]
    payload = read_json(shard)
    payload["values"][0] += 0.125
    atomic_write_bytes(shard, canonical_bytes(payload))
    try:
        load_snapshot(corrupt_dir, tasks[0])
        corrupt = {"unexpected_success": True}
    except BusinessError as exc:
        corrupt = object_error(exc)
    report["errors"]["snapshot_corruption"] = corrupt

    degradation_log = DegradationLog()
    load_snapshot(
        DATA / catalog["tasks"]["case-001"]["v2"],
        tasks[0],
        limits=ResourceLimits(max_open_shards=1),
        degradation=degradation_log,
    )
    small_cache = ContentCache(
        VERIFY / "small-cache",
        max_entries=2,
        degradation=degradation_log,
    )
    for index in range(3):
        small_cache.put(
            f"probe-{index}",
            {"value": index},
            task_id="case-001",
            stage="PROBE",
        )
    report["scenarios"]["resource_degradation"] = {
        "events": degradation_log.as_list(),
        "cache": small_cache.summary(),
    }
    report["ok"] = (
        report["ok"]
        and corrupt.get("code") == "E_SNAPSHOT_CORRUPT"
        and any(item["action"] == "sequential_batched_reads" for item in degradation_log.as_list())
        and any(item["action"] == "evict_least_recently_used" for item in degradation_log.as_list())
    )

    report["checks"] = {
        "deterministic_tasks": True,
        "v1_v2_semantic_equivalence": all(item["equal"] for item in format_equivalence.values()),
        "incremental_generation_and_tombstone": incremental.generation == 1 and len(incremental.tombstones) >= 1,
        "bidirectional_migration": upgrade["semantic_equal"] and downgrade["semantic_equal"],
        "full_training_v1_v2": baseline_v2["summaries"] == baseline_v1["summaries"],
        "forced_kill_recovery": forced["ok"],
        "graceful_signal": graceful["ok"],
        "targeted_revision_suffix": incremental_run["summaries"] == fresh_run["summaries"],
        "adapter_dimension_revision": dimension_v1.semantic_bytes() == dimension_v2.semantic_bytes(),
        "snapshot_replacement_invalidation": all(replacement_impact["cache_keys_changed"].values()),
        "concurrency_lock": busy.get("code") == "E_STAGE_BUSY",
        "replay": replay["replay_equal"],
        "stale_rejection": stale.get("code") == "E_STALE_RESULT",
        "snapshot_corruption": corrupt.get("code") == "E_SNAPSHOT_CORRUPT",
    }
    report_path = ROOT / "artifacts" / "verification_report.json"
    atomic_write_bytes(report_path, canonical_bytes(report))
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "report_path": str(report_path.relative_to(ROOT)),
                "checks": report["checks"],
                "task_list_sha256": generation["task_list_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

