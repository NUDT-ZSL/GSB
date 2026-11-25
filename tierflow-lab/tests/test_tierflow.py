from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tierflow.canonical import canonical_dumps
from tierflow.diagnostics import verify_project
from tierflow.errors import ClockRollbackError, PolicyRollbackError
from tierflow.ledger import build_object_ledger, compare_costs
from tierflow.policy import load_v1_policy, load_v2_policy, merge_policies
from tierflow.scheduler import recover_interrupted, run_migration
from tierflow.storage import StateStore, object_set_digest
from tierflow.validation import validate_config, validate_events, validate_manifest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SAMPLES = ROOT / "samples"
CLOCK = 1_700_000_600_000


@pytest.fixture(scope="session", autouse=True)
def generated_samples() -> None:
    if not (SAMPLES / "manifest.json").exists():
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_samples.py")],
            cwd=ROOT,
            check=True,
        )


@pytest.fixture()
def prepared_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "tierflow.db"
    manifest = validate_manifest(SAMPLES / "manifest.json")
    config = validate_config(SAMPLES / "config.json")
    events = validate_events(manifest, SAMPLES / "migration_events.json")
    store = StateStore(db_path)
    store.initialize_bundle(manifest, config, events, clock_ms=CLOCK, reset=True)
    v1 = load_v1_policy(SAMPLES / "policies" / "policy-v1.json", CLOCK)
    v2 = load_v2_policy(SAMPLES / "policies" / "v2", SAMPLES / "policies" / "v2" / "policy-v2-meta.sqlite")
    package = merge_policies(v1, v2, object_set_digest=object_set_digest(manifest["objects"]))
    store.apply_policy_package(package, applied_clock_ms=CLOCK)
    store.close()
    return db_path


def test_seed_validation_and_policy_merge_details() -> None:
    manifest = validate_manifest(SAMPLES / "manifest.json")
    config = validate_config(SAMPLES / "config.json")
    events = validate_events(manifest, SAMPLES / "migration_events.json")
    assert manifest["object_count"] == 12
    assert config["migration_byte_budget"] == 528 * 1024
    assert events["event_count"] == 8

    v1 = load_v1_policy(SAMPLES / "policies" / "policy-v1.json", CLOCK)
    v2 = load_v2_policy(SAMPLES / "policies" / "v2", SAMPLES / "policies" / "v2" / "policy-v2-meta.sqlite")
    package = merge_policies(v1, v2, object_set_digest=object_set_digest(manifest["objects"]))
    assert package["summary"]["shared_keys"] == 5
    assert package["summary"]["tombstoned_keys"] == 1
    entries = package["entries"]
    assert entries["heat.hot_threshold"]["source_format"] == "v1"
    conversion = entries["heat.hot_threshold"]["time_conversion"]
    assert conversion["rounded_delta_ms"] == 333
    assert conversion["rounding_direction"] == "floor"
    assert conversion["rounding_remainder_ms"] != "0"
    assert entries["heat.cold_threshold"]["source_format"] == "v2"
    assert "equal_logical_clock_source_priority" in entries["heat.cold_threshold"]["decision_basis"][0]
    assert "experimental.legacy_prefetch" not in entries


def test_fixed_point_ledger_flags_precision_sensitive_pair() -> None:
    manifest = validate_manifest(SAMPLES / "manifest.json")
    config = validate_config(SAMPLES / "config.json")
    by_key = {obj["key"]: obj for obj in manifest["objects"]}
    ledger_a = build_object_ledger(by_key["obj-0006"], config["cost_model"])
    ledger_b = build_object_ledger(by_key["obj-0007"], config["cost_model"])
    comparison = compare_costs(ledger_a, ledger_b, config["cost_model"]["difference_tolerance_scaled"])
    assert comparison["cost_difference_scaled"] == 256
    assert comparison["precision_sensitive"] is True
    assert len(ledger_a["error_source_chain"]) >= 3
    operations = [entry["operation"] for entry in ledger_a["entries"]]
    assert operations == ["byte_conversion", "multiply", "add"]


def test_full_schedule_verify_and_idempotent_repeat(prepared_db: Path) -> None:
    store = StateStore(prepared_db)
    first = run_migration(store, CLOCK, "round-a")
    assert first["status_counts"]["succeeded"] == 5
    assert first["status_counts"]["abandoned"] == 3
    assert first["outcome_counts"]["voluntarily_abandoned"] == 3
    assert first["watermarks"]["migration_committed"] == 5

    second = run_migration(store, CLOCK, "round-a")
    assert second["status_counts"]["succeeded"] == 5
    assert second["watermarks"]["migration_committed"] == 5
    jobs = store.list_jobs("round-a")
    assert len(jobs) == 8
    assert verify_project(SAMPLES, prepared_db)["ok"] is True
    store.close()


def test_bounded_batch_distinguishes_unstarted_category(prepared_db: Path) -> None:
    store = StateStore(prepared_db)
    report = run_migration(store, CLOCK, "batch", concurrency=1, max_launch=1)
    assert report["outcome_counts"]["succeeded"] == 1
    assert report["outcome_counts"]["within_budget_unstarted"] == 4
    assert report["outcome_counts"]["voluntarily_abandoned"] == 3
    store.close()


def test_started_failure_backoff_and_retry(prepared_db: Path) -> None:
    store = StateStore(prepared_db)
    report = run_migration(store, CLOCK, "fail", fail_objects={"obj-0005"})
    assert report["outcome_counts"]["started_failed"] == 1
    failed = next(job for job in report["jobs"] if job["object_key"] == "obj-0005")
    assert failed["backoff_until_clock_ms"] == CLOCK + 25
    recovered = recover_interrupted(store, CLOCK, "fail", retry_failed=True)
    assert recovered["report"]["status_counts"]["succeeded"] == 5
    assert verify_project(SAMPLES, prepared_db)["ok"] is True
    store.close()


def test_clock_rollback_returns_business_code(prepared_db: Path) -> None:
    from tierflow.scheduler import adjudicate

    store = StateStore(prepared_db)
    with pytest.raises(ClockRollbackError) as exc_info:
        adjudicate(store, CLOCK - 500_000)
    assert exc_info.value.numeric_code == 2001
    store.close()


def test_policy_rollback_error(prepared_db: Path) -> None:
    update = json.loads((SAMPLES / "policy_packages" / "rollback-v9.json").read_text())
    store = StateStore(prepared_db)
    current = json.loads(store.current_policy()["package_json"])
    package = {
        **update,
        "package_version": update["package_version"],
        "entries": {},
        "snapshot_digest": "rollback",
        "object_set_digest": store.get_meta("object_set_digest"),
    }
    with pytest.raises(PolicyRollbackError) as exc_info:
        store.apply_policy_package(package, applied_clock_ms=CLOCK, update_kind="full")
    assert exc_info.value.numeric_code == 3001
    assert current["manifest_version"] == 10
    store.close()


def test_force_kill_claim_then_resume_is_exactly_once(prepared_db: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        sys.executable,
        "-m",
        "tierflow.cli",
        "--samples",
        str(SAMPLES),
        "--db",
        str(prepared_db),
        "migrate",
        "--round-id",
        "crash",
        "--crash-after-claim",
        "obj-0006",
    ]
    crashed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert crashed.returncode == -9

    store = StateStore(prepared_db)
    before = store.watermarks().get("migration_committed", 0)
    assert before >= 0
    recovery = recover_interrupted(store, CLOCK, "crash")
    assert len(recovery["recovered_active_jobs"]) >= 1
    assert recovery["report"]["status_counts"]["succeeded"] == 5
    assert recovery["report"]["watermarks"]["migration_committed"] == 5

    again = recover_interrupted(store, CLOCK, "crash")
    assert again["recovered_active_jobs"] == []
    assert again["report"]["watermarks"]["migration_committed"] == 5
    assert verify_project(SAMPLES, prepared_db)["ok"] is True
    store.close()


def test_multiprocess_same_round_no_duplicate_or_overcommit(tmp_path: Path) -> None:
    db_path = tmp_path / "mp.db"
    manifest = validate_manifest(SAMPLES / "manifest.json")
    config = validate_config(SAMPLES / "config.json")
    events = validate_events(manifest, SAMPLES / "migration_events.json")
    store = StateStore(db_path)
    store.initialize_bundle(manifest, config, events, clock_ms=CLOCK, reset=True)
    v1 = load_v1_policy(SAMPLES / "policies" / "policy-v1.json", CLOCK)
    v2 = load_v2_policy(SAMPLES / "policies" / "v2", SAMPLES / "policies" / "v2" / "policy-v2-meta.sqlite")
    package = merge_policies(v1, v2, object_set_digest=object_set_digest(manifest["objects"]))
    store.apply_policy_package(package, applied_clock_ms=CLOCK)
    store.close()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    processes = []
    for _ in range(3):
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tierflow.cli",
                    "--samples",
                    str(SAMPLES),
                    "--db",
                    str(db_path),
                    "migrate",
                    "--round-id",
                    "mp",
                    "--workers",
                    "1",
                    "--task-delay",
                    "0.2",
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr + stdout

    conn = sqlite3.connect(db_path)
    duplicates = conn.execute(
        "SELECT object_key, COUNT(*) FROM migration_jobs WHERE round_id='mp' "
        "GROUP BY policy_version, object_key HAVING COUNT(*) > 1"
    ).fetchall()
    assert duplicates == []
    committed = conn.execute(
        "SELECT value_integer FROM watermarks WHERE name='migration_committed'"
    ).fetchone()[0]
    assert committed == 5
    overcommitted = conn.execute(
        "SELECT tier FROM capacity_accounts WHERE used_bytes + reserved_bytes > capacity_bytes"
    ).fetchall()
    assert overcommitted == []
    conn.close()
    assert verify_project(SAMPLES, db_path)["ok"] is True


def test_force_kill_after_prepare_half_commit_replays_cleanly(prepared_db: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        sys.executable,
        "-m",
        "tierflow.cli",
        "--samples",
        str(SAMPLES),
        "--db",
        str(prepared_db),
        "migrate",
        "--round-id",
        "half",
        "--workers",
        "1",
        "--crash-after-prepare",
        "obj-0006",
    ]
    crashed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert crashed.returncode == -9
    conn = sqlite3.connect(prepared_db)
    target_status = conn.execute(
        "SELECT status FROM migration_jobs WHERE round_id='half' AND object_key='obj-0006'"
    ).fetchone()[0]
    conn.close()
    assert target_status == "committing"

    store = StateStore(prepared_db)
    recovery = recover_interrupted(store, CLOCK, "half")
    assert any(item["job_key"].endswith("obj-0006") for item in recovery["recovered_active_jobs"])
    assert recovery["report"]["status_counts"]["succeeded"] == 5
    assert recovery["report"]["watermarks"]["migration_committed"] == 5
    assert verify_project(SAMPLES, prepared_db)["ok"] is True
    store.close()


def test_canonical_judge_output_is_byte_repeatable(prepared_db: Path) -> None:
    from tierflow.scheduler import adjudicate

    store = StateStore(prepared_db)
    first = canonical_dumps(adjudicate(store, CLOCK)["judgements"])
    second = canonical_dumps(adjudicate(store, CLOCK)["judgements"])
    assert first == second
    store.close()
