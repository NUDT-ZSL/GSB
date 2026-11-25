from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from layerforge.cachejudge import adjudicate_candidates
from layerforge.canonical import load_json, write_json_atomic
from layerforge.cli import main as cli_main
from layerforge.clock import LogicalClock
from layerforge.errors import (
    ClockRollbackError,
    GenerationRollbackError,
    LayerConflictError,
    PathEscapeError,
    SampleDigestConflictError,
    ShardHashMismatchError,
)
from layerforge.layers import object_path
from layerforge.manifestio import load_bundle
from layerforge.planner import derive_plan
from layerforge.runtime import make_paths, run_build, run_judge, run_verify
from layerforge.samplegen import generate_samples
from layerforge.store import Store
from layerforge import runtime as runtime_module


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "samples"
    generate_samples(path)
    return path


def runtime_paths(tmp_path: Path, sample_dir: Path, name: str = "run"):
    workdir = tmp_path / name
    return make_paths(
        sample_dir=sample_dir,
        workdir=workdir,
        report_dir=workdir / "reports",
    )


def test_seed_manifest_merge_and_sampling(sample_dir: Path, tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path, sample_dir, "judge")
    report = run_judge(paths)
    assert report["summary"]["total"] == 8
    assert report["summary"]["reuse"] == 8
    assert report["summary"]["sampling_steps"] == 1

    by_id = {item["step_id"]: item for item in report["steps"]}
    bench = by_id["bench-cache"]
    assert bench["sampling_mode"] == "bucket_sampling"
    assert "candidate_count=17" in bench["sampling_evidence"]["trigger"]
    assert bench["source_version"] == "v2"
    assert bench["cache_generation"] == 16
    assert 0 < bench["miss_rate_upper_bound"] < 1
    assert bench["sampling_evidence"]["risk"]["sampled_bucket_count"] == 10

    locale = by_id["locale-data"]
    assert locale["source_version"] == "v1"
    assert "higher_declared_logical_clock" in locale["merge_basis"]
    pkg = by_id["pkg-config"]
    assert pkg["merge_selected_source"] == "v2"
    assert pkg["merge_basis"] == "equal_logical_clock_format_priority_v2"


def test_build_verify_and_deterministic_object_tree(
    sample_dir: Path, tmp_path: Path
) -> None:
    first = runtime_paths(tmp_path, sample_dir, "first")
    second = runtime_paths(tmp_path, sample_dir, "second")
    first_report = run_build(first)
    second_report = run_build(second)
    assert first_report["ok"]
    assert second_report["ok"]
    assert run_verify(first)["ok"]
    assert run_verify(second)["ok"]

    first_manifest = (first.report_dir / "digests.json").read_bytes()
    second_manifest = (second.report_dir / "digests.json").read_bytes()
    assert first_manifest == second_manifest

    first_objects = {
        path.relative_to(first.object_dir).as_posix(): path.read_bytes()
        for path in first.object_dir.rglob("*.json")
    }
    second_objects = {
        path.relative_to(second.object_dir).as_posix(): path.read_bytes()
        for path in second.object_dir.rglob("*.json")
    }
    assert first_objects == second_objects


def test_parameter_invalidation_closure_and_unaffected_bytes(
    sample_dir: Path, tmp_path: Path
) -> None:
    baseline = runtime_paths(tmp_path, sample_dir, "baseline")
    changed = runtime_paths(tmp_path, sample_dir, "changed")
    assert run_build(baseline)["ok"]

    judge = run_judge(
        changed,
        overrides={"BUILD_CHANNEL": "canary"},
        write=False,
    )
    chain = judge["invalidation_chain"]
    assert chain["root_steps"] == ["app-layer"]
    assert chain["affected_steps"] == ["app-layer", "final-image", "bench-cache"]
    assert set(chain["step_generations"]) == set(chain["affected_steps"])

    changed_report = run_build(changed, overrides={"BUILD_CHANNEL": "canary"})
    assert changed_report["ok"]
    verify = run_verify(changed)
    assert verify["ok"]

    baseline_plan = derive_plan(load_bundle(sample_dir))["by_id"]
    changed_plan = derive_plan(
        load_bundle(sample_dir), parameter_overrides={"BUILD_CHANNEL": "canary"}
    )["by_id"]
    affected = set(chain["affected_steps"])
    for step_id, item in baseline_plan.items():
        baseline_file = object_path(baseline.object_dir, item["layer_digest"])
        assert baseline_file.exists()
        if step_id in affected:
            assert item["layer_digest"] != changed_plan[step_id]["layer_digest"]
            changed_file = object_path(changed.object_dir, changed_plan[step_id]["layer_digest"])
            assert changed_file.exists()
        else:
            assert item["layer_digest"] == changed_plan[step_id]["layer_digest"]
            changed_file = object_path(changed.object_dir, item["layer_digest"])
            assert baseline_file.read_bytes() == changed_file.read_bytes()


def test_budget_abandons_remaining_then_resume_matches_reference(
    sample_dir: Path, tmp_path: Path
) -> None:
    reference = runtime_paths(tmp_path, sample_dir, "reference")
    budget = runtime_paths(tmp_path, sample_dir, "budget")
    assert run_build(reference)["ok"]

    report = run_build(budget, concurrency=2, round_budget_ms=4)
    assert report["status"] == "budget_abandoned"
    assert report["exit_code"] == 30
    abandoned = {item["step_id"]: item for item in report["schedule"]["abandoned"]}
    assert "locale-data" in abandoned
    assert abandoned["locale-data"]["category"] == "abandoned_budget"

    plan = derive_plan(load_bundle(sample_dir))["by_id"]
    assert object_path(budget.object_dir, plan["pkg-config"]["layer_digest"]).exists()

    deferred_paths = runtime_paths(tmp_path, sample_dir, "deferred")
    deferred_report = run_build(deferred_paths, concurrency=1, round_budget_ms=20)
    assert deferred_report["ok"]
    conn = sqlite3.connect(deferred_paths.state_db)
    try:
        deferred_events = conn.execute(
            "SELECT COUNT(*) FROM schedule_events WHERE category='not_started_within_budget'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert deferred_events >= 1

    resumed = run_build(budget, concurrency=2, round_budget_ms=20)
    assert resumed["ok"]
    verification = run_verify(budget, reference_dir=reference.workdir)
    assert verification["ok"]
    assert verification["reference_check"]["digests_byte_identical"]


def test_crash_after_rename_recovers_byte_identical_tree(
    sample_dir: Path, tmp_path: Path
) -> None:
    reference = runtime_paths(tmp_path, sample_dir, "reference")
    crashed = runtime_paths(tmp_path, sample_dir, "crashed")
    assert run_build(reference)["ok"]

    command = [
        sys.executable,
        "-m",
        "layerforge",
        "build",
        "--sample-dir",
        str(sample_dir),
        "--workdir",
        str(crashed.workdir),
        "--report-dir",
        str(crashed.report_dir),
        "--crash-step",
        "final-image",
        "--crash-at",
        "object_renamed",
    ]
    crashed_process = subprocess.run(command, cwd=REPO_ROOT, check=False)
    assert crashed_process.returncode == 88

    recovered = run_build(crashed)
    assert recovered["ok"]
    assert any(item["step_id"] == "final-image" for item in recovered["recovered"])
    verification = run_verify(crashed, reference_dir=reference.workdir)
    assert verification["ok"]
    assert verification["reference_check"]["digests_byte_identical"]


@pytest.mark.parametrize("crash_at", ["temp_object", "cache_commit"])
def test_other_crash_points_recover(
    sample_dir: Path, tmp_path: Path, crash_at: str
) -> None:
    reference = runtime_paths(tmp_path, sample_dir, f"reference-{crash_at}")
    crashed = runtime_paths(tmp_path, sample_dir, f"crashed-{crash_at}")
    assert run_build(reference)["ok"]
    command = [
        sys.executable,
        "-m",
        "layerforge",
        "build",
        "--sample-dir",
        str(sample_dir),
        "--workdir",
        str(crashed.workdir),
        "--report-dir",
        str(crashed.report_dir),
        "--crash-step",
        "pkg-config",
        "--crash-at",
        crash_at,
    ]
    assert subprocess.run(command, cwd=REPO_ROOT, check=False).returncode == 88
    recovered = run_build(crashed)
    assert recovered["ok"]
    verification = run_verify(crashed, reference_dir=reference.workdir)
    assert verification["ok"]
    assert verification["reference_check"]["digests_byte_identical"]


def test_failed_after_start_distinguished_from_not_started(
    sample_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = runtime_paths(tmp_path, sample_dir, "failure")

    def failing_materialize(item, **kwargs):
        if item["step_id"] == "pkg-config":
            raise RuntimeError("injected materialization failure")
        raise AssertionError("only pkg-config should be admitted in round one")

    monkeypatch.setattr(runtime_module, "materialize_item", failing_materialize)
    report = run_build(paths, concurrency=1, round_budget_ms=20)
    assert report["status"] == "failed"
    assert report["exit_code"] == 31
    failed_ids = {item["step_id"] for item in report["schedule"]["failed"]}
    not_started = {item["step_id"]: item for item in report["schedule"]["not_started_in_budget"]}
    assert failed_ids == {"pkg-config"}
    assert not_started["locale-data"]["category"] == "not_started_within_budget"


def test_multiprocess_same_step_is_materialized_once(
    sample_dir: Path, tmp_path: Path
) -> None:
    paths = runtime_paths(tmp_path, sample_dir, "mp")
    base_command = [
        sys.executable,
        "-m",
        "layerforge",
        "worker",
        "--sample-dir",
        str(sample_dir),
        "--workdir",
        str(paths.workdir),
        "--report-dir",
        str(paths.report_dir),
    ]
    processes = [
        subprocess.Popen([*base_command, "--step", "pkg-config", "--owner", "A"], cwd=REPO_ROOT),
        subprocess.Popen([*base_command, "--step", "pkg-config", "--owner", "B"], cwd=REPO_ROOT),
        subprocess.Popen([*base_command, "--step", "locale-data", "--owner", "C"], cwd=REPO_ROOT),
    ]
    codes = [process.wait(timeout=30) for process in processes]
    assert codes == [0, 0, 0]

    conn = sqlite3.connect(paths.state_db)
    try:
        pkg_rows = conn.execute(
            "SELECT COUNT(*) FROM materializations WHERE step_id='pkg-config' AND status='committed'"
        ).fetchone()[0]
        pkg_cache = conn.execute(
            "SELECT COUNT(*) FROM cache_entries WHERE step_id='pkg-config'"
        ).fetchone()[0]
        locale_rows = conn.execute(
            "SELECT COUNT(*) FROM materializations WHERE step_id='locale-data' AND status='committed'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert pkg_rows == 1
    assert pkg_cache == 1
    assert locale_rows == 1


def test_validation_error_codes_and_diagnostics(sample_dir: Path) -> None:
    v1_path = sample_dir / "v1_manifest.json"
    v1 = load_json(v1_path)

    bad_path = sample_dir / "bad-path"
    bad_path.mkdir()
    bad_sample = sample_dir / "path-case"
    import shutil

    shutil.copytree(sample_dir, bad_sample, dirs_exist_ok=True)
    manifest = load_json(bad_sample / "v1_manifest.json")
    manifest["steps"][0]["actions"].append(
        {"op": "add", "path": "../escape.txt", "content": "no escape"}
    )
    write_json_atomic(bad_sample / "v1_manifest.json", manifest)
    with pytest.raises(PathEscapeError) as exc:
        load_bundle(bad_sample)
    assert exc.value.details["path"] == "../escape.txt"
    assert cli_main(["judge", "--sample-dir", str(bad_sample), "--workdir", str(sample_dir.parent / "cli-bad-path")]) == 20

    missing_base = sample_dir / "missing-base"
    shutil.copytree(sample_dir, missing_base)
    manifest = load_json(missing_base / "v1_manifest.json")
    manifest["steps"][0]["base_ref"] = "base:does-not-exist"
    write_json_atomic(missing_base / "v1_manifest.json", manifest)
    with pytest.raises(Exception) as exc:
        load_bundle(missing_base)
    assert exc.value.code == "E_BASE_LAYER_MISSING"

    conflict = sample_dir / "conflict"
    shutil.copytree(sample_dir, conflict)
    manifest = load_json(conflict / "v1_manifest.json")
    manifest["steps"][0]["actions"][0]["content"] += "tampered\n"
    write_json_atomic(conflict / "v1_manifest.json", manifest)
    with pytest.raises(LayerConflictError):
        load_bundle(conflict)

    bad_shard = sample_dir / "bad-shard"
    shutil.copytree(sample_dir, bad_shard)
    index = load_json(bad_shard / "v2_manifest.json")
    index["shards"][0]["sha256"] = "sha256:" + "0" * 64
    write_json_atomic(bad_shard / "v2_manifest.json", index)
    with pytest.raises(ShardHashMismatchError):
        load_bundle(bad_shard)

    bad_generation = sample_dir / "bad-generation"
    shutil.copytree(sample_dir, bad_generation)
    conn = sqlite3.connect(bad_generation / "v2_metadata.sqlite3")
    conn.execute(
        "UPDATE cache_entries SET generation=1 WHERE step_id='pkg-config' AND candidate_kind='current'"
    )
    conn.commit()
    conn.close()
    with pytest.raises(GenerationRollbackError):
        load_bundle(bad_generation)


def test_clock_and_sampling_conflict_error_codes() -> None:
    clock = LogicalClock(100, tolerance_ms=2)
    with pytest.raises(ClockRollbackError) as exc:
        clock.observe(90)
    assert exc.value.details["backward_ms"] == 10

    candidates = [
        {
            "cache_key": "same-key",
            "content_digest": "sha256:" + "a" * 64,
            "source_version": "v2",
            "cache_generation": 2,
        },
        {
            "cache_key": "same-key",
            "content_digest": "sha256:" + "b" * 64,
            "source_version": "v1",
            "cache_generation": 1,
        },
    ]
    with pytest.raises(SampleDigestConflictError):
        adjudicate_candidates(
            step_id="conflict",
            target_cache_key="target",
            target_content_digest="sha256:" + "c" * 64,
            candidates=candidates,
            config={"exact_candidate_threshold": 12, "exact_object_threshold": 24},
        )


def test_cli_status_and_verify_commands(sample_dir: Path, tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path, sample_dir, "cli")
    assert cli_main([
        "build",
        "--sample-dir",
        str(sample_dir),
        "--workdir",
        str(paths.workdir),
        "--report-dir",
        str(paths.report_dir),
    ]) == 0
    assert cli_main([
        "status",
        "--sample-dir",
        str(sample_dir),
        "--workdir",
        str(paths.workdir),
        "--report-dir",
        str(paths.report_dir),
    ]) == 0
    assert cli_main([
        "verify",
        "--sample-dir",
        str(sample_dir),
        "--workdir",
        str(paths.workdir),
        "--report-dir",
        str(paths.report_dir),
    ]) == 0
    assert cli_main([
        "build",
        "--sample-dir",
        str(sample_dir),
        "--workdir",
        str(paths.workdir),
        "--report-dir",
        str(paths.report_dir),
        "--clock-ms",
        "1",
    ]) == 25
