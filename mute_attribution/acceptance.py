from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np

import mute_attribution
from .attribution import compute_attribution_record
from .config import load_config
from .consistency import run_consistency
from .errors import BusinessError
from .features import compute_feature_record, load_manifest
from .lineage import check_partition, replay_event
from .noise import compute_noise_record
from .pipeline import Pipeline
from .profile import validate_events
from .store import SnapshotStore
from .synth import edit_truth, generate_dataset, replace_pcm_gain
from .util import atomic_write_json, canonical_json, read_json, sha256_json


def _snapshot_bytes(store_root: Path, stage: str, version: int) -> bytes:
    return (
        store_root
        / "stages"
        / stage
        / "versions"
        / f"v{version:06d}"
        / "snapshot.json"
    ).read_bytes()


def _stage_report(report: dict[str, Any], stage: str) -> dict[str, Any]:
    return next(item for item in report["stages"] if item["stage"] == stage)


def _expected_business_error(
    code: str, action: Callable[[], Any]
) -> dict[str, Any]:
    try:
        action()
    except BusinessError as exc:
        if exc.code != code:
            raise AssertionError(f"expected {code}, got {exc.code}: {exc.context}")
        return exc.to_dict()
    raise AssertionError(f"expected business error {code}, but action succeeded")


def _count_pending(store_root: Path) -> int:
    work = store_root / "work"
    if not work.exists():
        return 0
    return sum(1 for _ in work.rglob("*"))


def run_acceptance(work_dir: Path, artifact_dir: Path) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parent.parent
    work_dir = work_dir.resolve()
    artifact_dir = artifact_dir.resolve()
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    baseline_data = work_dir / "baseline_data"
    generation = generate_dataset(baseline_data, seed=20261122, calls=3)
    config = load_config(project_root / "configs" / "default.json")
    calls = load_manifest(baseline_data)
    call_map = {call.call_id: call for call in calls}
    checks: list[dict[str, Any]] = []

    baseline_store_root = work_dir / "baseline_store"
    baseline_store = SnapshotStore(baseline_store_root)
    baseline_report = Pipeline(baseline_data, baseline_store, config).run()
    baseline_versions = baseline_report["versions"]

    def check_baseline() -> dict[str, Any]:
        if baseline_report["code"] != "OK":
            raise AssertionError("baseline run did not complete")
        attr_snapshot = baseline_store.get_current("attribution")
        assert attr_snapshot is not None
        labels_per_call: dict[str, dict[str, int]] = {}
        expected_counts = {
            "call_0001": {
                "stationary_floor": 3,
                "device_step": 1,
                "crosstalk_residue": 1,
            },
            "call_0002": {
                "stationary_floor": 5,
                "device_step": 1,
                "crosstalk_residue": 1,
            },
            "call_0003": {
                "stationary_floor": 6,
                "device_step": 1,
                "crosstalk_residue": 1,
            },
        }
        for record in attr_snapshot.records:
            counts: dict[str, int] = {}
            for segment in record["silence_segments"]:
                label = segment["label"]
                counts[label] = counts.get(label, 0) + 1
            labels_per_call[record["call_id"]] = counts
            if counts != expected_counts[record["call_id"]]:
                raise AssertionError(
                    f"{record['call_id']} labels {counts} != "
                    f"{expected_counts[record['call_id']]}"
                )
        consistency = run_consistency(baseline_store, calls)
        return {
            "labels_per_call": labels_per_call,
            "versions": baseline_versions,
            "consistency_code": consistency["code"],
            "lineage_replayed_events": consistency["lineage_replayed_events"],
        }

    checks.append({"check": "baseline_attribution", "code": "OK", "detail": check_baseline()})

    deterministic_store = SnapshotStore(work_dir / "deterministic_store")
    deterministic_report = Pipeline(baseline_data, deterministic_store, config).run()

    def check_determinism() -> dict[str, Any]:
        equal: dict[str, bool] = {}
        for stage in ("features", "noise_floor", "attribution", "profile"):
            version = int(deterministic_report["versions"][stage])
            equal[stage] = _snapshot_bytes(
                baseline_store_root, stage, version
            ) == _snapshot_bytes(work_dir / "deterministic_store", stage, version)
        if not all(equal.values()):
            raise AssertionError(f"snapshot bytes differ: {equal}")
        return {
            "byte_identical_stage_snapshots": equal,
            "feature_frame_counts": {
                record["call_id"]: record["frame_count"]
                for record in deterministic_store.get_current("features").records
            },
        }

    checks.append({"check": "deterministic_output", "code": "OK", "detail": check_determinism()})

    crash_store_root = work_dir / "crash_store"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "mute_attribution",
            "run",
            "--data-dir",
            str(baseline_data),
            "--store",
            str(crash_store_root),
            "--config",
            str(project_root / "configs" / "default.json"),
            "--crash-at",
            "noise_floor:1",
        ],
        cwd=str(project_root),
        env={**os.environ, "PYTHONPATH": str(project_root)},
        capture_output=True,
        text=True,
    )
    if process.returncode != 137:
        raise AssertionError(
            f"crash process returned {process.returncode}: {process.stderr}"
        )
    crash_state = read_json(crash_store_root / "state.json")
    pending_at_crash = _count_pending(crash_store_root)
    recovery_report = Pipeline(
        baseline_data, SnapshotStore(crash_store_root), config
    ).run()

    def check_recovery() -> dict[str, Any]:
        if pending_at_crash < 1:
            raise AssertionError("no pending intermediate work existed after crash")
        if list(crash_state["stages"]) != ["features"]:
            raise AssertionError(f"unexpected crash state: {crash_state['stages']}")
        if _count_pending(crash_store_root) != 0:
            raise AssertionError("pending work was not discarded on recovery")
        equal: dict[str, bool] = {}
        for stage in ("features", "noise_floor", "attribution", "profile"):
            version = int(recovery_report["versions"][stage])
            equal[stage] = _snapshot_bytes(
                baseline_store_root, stage, version
            ) == _snapshot_bytes(crash_store_root, stage, version)
        if not all(equal.values()):
            raise AssertionError(f"recovered snapshots differ: {equal}")
        if recovery_report["recovery"]["resumed_from"] != "features":
            raise AssertionError("recovery did not resume from features")
        if recovery_report["recovery"]["rebuilt_stages"] != [
            "noise_floor",
            "attribution",
            "profile",
        ]:
            raise AssertionError(
                f"unexpected rebuilt stages: {recovery_report['recovery']}"
            )
        return {
            "crash_exit_code": process.returncode,
            "pending_entries_at_crash": pending_at_crash,
            "pending_entries_after_recovery": _count_pending(crash_store_root),
            "recovery": recovery_report["recovery"],
            "byte_identical_stage_snapshots": equal,
            "recovery_stages": [
                {
                    "stage": stage["stage"],
                    "status": stage["status"],
                    "version": stage["version"],
                }
                for stage in recovery_report["stages"]
            ],
        }

    checks.append({"check": "forced_termination_recovery", "code": "OK", "detail": check_recovery()})

    migration_data = work_dir / "migration_data"
    migration_store_root = work_dir / "migration_store"
    shutil.copytree(baseline_data, migration_data)
    shutil.copytree(baseline_store_root, migration_store_root)
    migration_store = SnapshotStore(migration_store_root)
    bumped_config = json.loads(canonical_json(config))
    bumped_config["noise_floor"]["version"] = 2
    bump_report = Pipeline(migration_data, migration_store, bumped_config).run()

    def check_bump() -> dict[str, Any]:
        if _stage_report(bump_report, "features")["status"] != "reused":
            raise AssertionError("features stage was not reused")
        if bump_report["versions"]["noise_floor"] != 2:
            raise AssertionError("noise floor version did not advance")
        if bump_report["versions"]["attribution"] != 2:
            raise AssertionError("attribution version did not advance")
        if bump_report["versions"]["profile"] != 2:
            raise AssertionError("profile version did not advance")
        archived = sorted(
            str(path.relative_to(migration_store_root))
            for path in migration_store_root.glob("archive/*/v*/snapshot.json")
        )
        expected = [
            "archive/attribution/v000001/snapshot.json",
            "archive/noise_floor/v000001/snapshot.json",
            "archive/profile/v000001/snapshot.json",
        ]
        if archived != expected:
            raise AssertionError(f"archived snapshots {archived} != {expected}")
        return {
            "active_versions": bump_report["versions"],
            "archived_snapshots": archived,
            "replaced_stages": [
                report["stage"]
                for report in bump_report["stages"]
                if report["status"] != "reused"
            ],
            "reused_stage": "features",
        }

    checks.append({"check": "config_bump_snapshot_replacement", "code": "OK", "detail": check_bump()})

    truth_result = edit_truth(
        migration_data, "call_0001", 216, 228, "speech"
    )
    truth_report = Pipeline(migration_data, migration_store, bumped_config).run()

    def check_truth() -> dict[str, Any]:
        if _stage_report(truth_report, "features")["status"] != "reused":
            raise AssertionError("features stage was not reused for truth edit")
        if _stage_report(truth_report, "noise_floor")["status"] != "reused":
            raise AssertionError("noise floor stage was not reused for truth edit")
        attr = _stage_report(truth_report, "attribution")
        profile = _stage_report(truth_report, "profile")
        if attr["recomputed_calls"] != ["call_0001"]:
            raise AssertionError(f"unexpected attribution calls: {attr['recomputed_calls']}")
        if profile["recomputed_calls"] != ["call_0001"]:
            raise AssertionError(f"unexpected profile calls: {profile['recomputed_calls']}")
        if attr["affected_frame_ranges"] != {"call_0001": [[220, 228]]}:
            raise AssertionError(f"unexpected frame ranges: {attr['affected_frame_ranges']}")
        return {
            "truth_revision": truth_result["revision"],
            "attribution_version": truth_report["versions"]["attribution"],
            "profile_version": truth_report["versions"]["profile"],
            "features_status": _stage_report(truth_report, "features")["status"],
            "noise_floor_status": _stage_report(truth_report, "noise_floor")["status"],
            "recomputed_calls": attr["recomputed_calls"],
            "reused_calls": attr["reused_calls"],
            "affected_frame_ranges": attr["affected_frame_ranges"],
        }

    checks.append({"check": "truth_edit_targeted_invalidation", "code": "OK", "detail": check_truth()})

    replace_pcm_gain(migration_data, "call_0002", 1.1)
    pcm_report = Pipeline(migration_data, migration_store, bumped_config).run()

    def check_pcm_replacement() -> dict[str, Any]:
        for stage in ("features", "noise_floor", "attribution", "profile"):
            report = _stage_report(pcm_report, stage)
            if report["recomputed_calls"] != ["call_0002"]:
                raise AssertionError(
                    f"{stage} recomputed {report['recomputed_calls']}, expected call_0002"
                )
        consistency = run_consistency(migration_store, calls)
        return {
            "versions": pcm_report["versions"],
            "recomputed_calls_by_stage": {
                stage: _stage_report(pcm_report, stage)["recomputed_calls"]
                for stage in ("features", "noise_floor", "attribution", "profile")
            },
            "consistency_code": consistency["code"],
        }

    checks.append({"check": "pcm_replacement_suffix", "code": "OK", "detail": check_pcm_replacement()})

    baseline_profile = baseline_store.get_current("profile")
    assert baseline_profile is not None
    first_event = baseline_profile.records[0]["events"][0]
    replay_result = replay_event(
        baseline_store, first_event, call_map[first_event["call_id"]]
    )

    def check_lineage() -> dict[str, Any]:
        if replay_result["label"] != first_event["label"]:
            raise AssertionError("replay label differs")
        if replay_result["deviation_db"] != first_event["deviation_db"]:
            raise AssertionError("replay deviation differs")
        return {
            "event_id": first_event["event_id"],
            "label": replay_result["label"],
            "deviation_db": replay_result["deviation_db"],
            "lineage": first_event["lineage"],
            "replayed_segments": replay_result["replayed_segments"],
        }

    checks.append({"check": "lineage_replay", "code": "OK", "detail": check_lineage()})

    baseline_features = baseline_store.get_current("features")
    baseline_noise = baseline_store.get_current("noise_floor")
    assert baseline_features is not None and baseline_noise is not None
    feature_record = baseline_features.records[0]
    noise_record = baseline_noise.records[0]
    truth = read_json(baseline_data / "truth" / "call_0001.json")
    bad_truth = dict(truth)
    bad_truth["frame_count"] = 236

    error_data = work_dir / "error_data"
    shutil.copytree(baseline_data, error_data)
    observations_path = error_data / "observations" / "call_0001.jsonl"
    with observations_path.open("a", encoding="utf-8") as handle:
        handle.write(
            canonical_json(
                {
                    **read_json(
                        error_data / "truth" / "call_0001.json"
                    ),
                    "frame_index": 240,
                }
            )
            + "\n"
        )

    demos: dict[str, dict[str, Any]] = {}
    demos["E_FRAME_MISMATCH"] = _expected_business_error(
        "E_FRAME_MISMATCH",
        lambda: compute_feature_record(
            call_map["call_0001"], error_data, config, 99
        ),
    )
    demos["E_WINDOW_INVALID"] = _expected_business_error(
        "E_WINDOW_INVALID",
        lambda: compute_noise_record(
            feature_record,
            {
                **config["noise_floor"],
                "window_frames": int(feature_record["frame_count"]) + 1,
                "step_frames": 5,
            },
            config,
            99,
        ),
    )
    demos["E_STEP_INVALID"] = _expected_business_error(
        "E_STEP_INVALID",
        lambda: compute_noise_record(
            feature_record,
            {
                **config["noise_floor"],
                "window_frames": 25,
                "step_frames": 0,
            },
            config,
            99,
        ),
    )
    demos["E_LABEL_INVALID"] = _expected_business_error(
        "E_LABEL_INVALID",
        lambda: compute_attribution_record(
            call_map["call_0001"],
            feature_record,
            noise_record,
            truth,
            config,
            99,
            "lineage-test",
            forced_label="uncontrolled_noise",
        ),
    )
    demos["E_SEGMENT_SPLIT"] = _expected_business_error(
        "E_SEGMENT_SPLIT",
        lambda: compute_attribution_record(
            call_map["call_0001"],
            feature_record,
            noise_record,
            bad_truth,
            config,
            99,
            "lineage-test",
        ),
    )
    demos["E_EVENT_RANGE"] = _expected_business_error(
        "E_EVENT_RANGE",
        lambda: validate_events(
            [
                {
                    "event_id": "call_0001-E999",
                    "start_frame": 0,
                    "end_frame": 9999,
                }
            ],
            call_map["call_0001"],
        ),
    )
    demos["E_EVENT_OVERLAP"] = _expected_business_error(
        "E_EVENT_OVERLAP",
        lambda: validate_events(
            [
                {
                    "event_id": "call_0001-E001",
                    "start_frame": 0,
                    "end_frame": 10,
                },
                {
                    "event_id": "call_0001-E002",
                    "start_frame": 5,
                    "end_frame": 12,
                },
            ],
            call_map["call_0001"],
        ),
    )
    demos["E_SNAPSHOT_ROLLBACK"] = _expected_business_error(
        "E_SNAPSHOT_ROLLBACK",
        lambda: baseline_store.attempt_rollback(
            "features", baseline_versions["features"]
        ),
    )
    corrupt_store_root = work_dir / "corrupt_store"
    shutil.copytree(baseline_store_root, corrupt_store_root)
    corrupt_path = (
        corrupt_store_root
        / "stages"
        / "features"
        / "versions"
        / "v000001"
        / "snapshot.json"
    )
    corrupt_payload = read_json(corrupt_path)
    corrupt_payload["tampered"] = True
    atomic_write_json(corrupt_path, corrupt_payload)
    demos["E_SNAPSHOT_CORRUPT"] = _expected_business_error(
        "E_SNAPSHOT_CORRUPT",
        lambda: SnapshotStore(corrupt_store_root).get_current("features"),
    )
    bad_event = json.loads(canonical_json(first_event))
    bad_event["lineage"]["snapshot_version"] = None
    demos["E_LINEAGE_BROKEN"] = _expected_business_error(
        "E_LINEAGE_BROKEN",
        lambda: replay_event(
            baseline_store, bad_event, call_map[bad_event["call_id"]]
        ),
    )
    demos["E_INCONSISTENT"] = _expected_business_error(
        "E_INCONSISTENT",
        lambda: check_partition(["call_0001"], ["call_0001"], ["call_0001"]),
    )

    if set(demos) != {
        "E_FRAME_MISMATCH",
        "E_WINDOW_INVALID",
        "E_STEP_INVALID",
        "E_LABEL_INVALID",
        "E_SEGMENT_SPLIT",
        "E_EVENT_RANGE",
        "E_EVENT_OVERLAP",
        "E_SNAPSHOT_ROLLBACK",
        "E_SNAPSHOT_CORRUPT",
        "E_LINEAGE_BROKEN",
        "E_INCONSISTENT",
    }:
        raise AssertionError("business code demonstration coverage is incomplete")

    report: dict[str, Any] = {
        "code": "OK",
        "application": "mute-attribution",
        "version": mute_attribution.__version__,
        "environment": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "platform": platform.platform(),
            "sample_rate": generation["sample_rate"],
            "frame_ms": generation["frame_ms"],
            "seed": generation["seed"],
            "calls": generation["calls"],
            "container_used": False,
        },
        "checks": checks,
        "business_code_demonstrations": demos,
        "summary": {
            "checks_passed": len(checks),
            "business_codes_verified": len(demos),
            "baseline_versions": baseline_versions,
            "migration_versions": pcm_report["versions"],
            "recovered_snapshots_byte_identical": checks[2]["detail"][
                "byte_identical_stage_snapshots"
            ],
        },
    }
    atomic_write_json(artifact_dir / "acceptance_report.json", report)
    return report

