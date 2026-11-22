from __future__ import annotations

from pathlib import Path

import pytest

from dynabatch.errors import BusinessError
from dynabatch.scheduler import SchedulerEngine
from dynabatch.status import build_status, verify_store
from dynabatch.store import Store
from dynabatch.validation import load_bundle, validate_bundle


ROOT = Path(__file__).resolve().parents[1]


def test_noop_incremental_and_rollback(tmp_path, sample_bundle, clone_bundle) -> None:
    bundle = validate_bundle(sample_bundle)
    with Store(tmp_path / "state.db", create=True) as store:
        created = store.ingest_bundle(bundle)
        assert created["action"] == "created"
        assert created["current_replica_version"] == 3
        assert [item["classification"] for item in created["transitions"]] == [
            "complete_replace",
            "incremental_append",
            "complete_replace",
        ]
        noop = store.ingest_bundle(bundle)
        assert noop["action"] == "noop"
        assert noop["versions_added"] == []

        incremental = clone_bundle(sample_bundle)
        new_version = {
            "version": 4,
            "effective_at_ms": 180,
            "replicas": list(incremental["replica_versions"][-1]["replicas"])
            + [
                {
                    "replica_id": "r-d",
                    "compute_units": 1.1,
                    "memory_mib": 96,
                    "batch_window_ms": 20,
                    "max_batch_size": 3,
                    "max_sequence_length": 64,
                    "max_concurrent_batches": 2,
                }
            ],
        }
        incremental["replica_versions"].append(new_version)
        updated = store.ingest_bundle(validate_bundle(incremental))
        assert updated["current_replica_version"] == 4
        assert updated["transitions"][-1]["classification"] == "incremental_append"

        rollback = load_bundle(ROOT / "sample" / "rollback_bundle.json")
        with pytest.raises(BusinessError) as exc:
            store.ingest_bundle(rollback)
        assert exc.value.code == "E_VERSION_ROLLBACK"
        assert store.current_version_number() == 4


def test_current_version_conflict_is_rejected(tmp_path, sample_bundle, clone_bundle) -> None:
    bundle = validate_bundle(sample_bundle)
    with Store(tmp_path / "state.db", create=True) as store:
        store.ingest_bundle(bundle)
        conflict = clone_bundle(sample_bundle)
        conflict["replica_versions"][-1]["replicas"][0]["memory_mib"] = 128
        with pytest.raises(BusinessError) as exc:
            store.ingest_bundle(validate_bundle(conflict))
        assert exc.value.code == "E_VERSION_CONFLICT"
        assert store.current_version_number() == 3


def test_window_duration_comes_from_bound_version(
    tmp_path, tiny_bundle, clone_bundle
) -> None:
    bundle = clone_bundle(tiny_bundle)
    bundle["replica_versions"].append(
        {
            "version": 2,
            "effective_at_ms": 30,
            "replicas": [
                {
                    "replica_id": "r0",
                    "compute_units": 1.2,
                    "memory_mib": 80,
                    "batch_window_ms": 40,
                    "max_batch_size": 4,
                    "max_sequence_length": 32,
                    "max_concurrent_batches": 2,
                }
            ],
        }
    )
    bundle["requests"] = [
        {
            "seq": 1,
            "arrival_ms": 0,
            "priority": 0,
            "input_tokens": 4,
            "max_output_tokens": 4,
            "deadline_ms": 200,
        },
        {
            "seq": 2,
            "arrival_ms": 10,
            "priority": 1,
            "input_tokens": 5,
            "max_output_tokens": 4,
            "deadline_ms": 200,
        },
        {
            "seq": 3,
            "arrival_ms": 25,
            "priority": 2,
            "input_tokens": 6,
            "max_output_tokens": 4,
            "deadline_ms": 200,
        },
        {
            "seq": 4,
            "arrival_ms": 35,
            "priority": 0,
            "input_tokens": 4,
            "max_output_tokens": 5,
            "deadline_ms": 200,
        },
        {
            "seq": 5,
            "arrival_ms": 45,
            "priority": 1,
            "input_tokens": 5,
            "max_output_tokens": 5,
            "deadline_ms": 200,
        },
    ]
    with Store(tmp_path / "state.db", create=True) as store:
        store.ingest_bundle(validate_bundle(bundle))
        with store.lock():
            SchedulerEngine(store).run(100)
        status = build_status(store)
        versions = {item["version"]: item for item in status["replica_versions"]}
        assert {window["replica_version"] for window in status["windows"]} == {1, 2}
        for window in status["windows"]:
            replica = versions[window["replica_version"]]["replicas"][0]
            assert window["end_ms"] - window["start_ms"] == replica["batch_window_ms"]
            assert window["start_ms"] % replica["batch_window_ms"] == 0
        assert status["stats"]["pending_requests"] == 0
        verification = verify_store(store)
        assert verification["ok"], verification["errors"]
