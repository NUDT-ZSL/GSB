from __future__ import annotations

from dynabatch.scheduler import SchedulerEngine
from dynabatch.status import build_status, verify_store
from dynabatch.store import Store
from dynabatch.validation import validate_bundle


def _schedule_sample(tmp_path, sample_bundle):
    bundle = validate_bundle(sample_bundle)
    store = Store(tmp_path / "state.db", create=True)
    store.ingest_bundle(bundle)
    with store.lock():
        report = SchedulerEngine(store).run(300)
    return store, report


def test_windows_batches_versions_and_evidence(tmp_path, sample_bundle) -> None:
    store, report = _schedule_sample(tmp_path, sample_bundle)
    try:
        status = build_status(store)
        assert report["windows_committed"] > 0
        assert report["batches_committed"] > 0
        assert status["stats"]["pending_requests"] == 0
        assert status["stats"]["request_status_counts"] == {"BATCHED": 18}
        assert status["window_watermark_by_replica"]
        assert {item["replica_version"] for item in status["windows"]} >= {1, 2, 3}

        assignment_counts = {}
        for assignment in status["assignments"]:
            assert assignment["state"] == "committed"
            assignment_counts[assignment["request_seq"]] = (
                assignment_counts.get(assignment["request_seq"], 0) + 1
            )
        assert set(assignment_counts.values()) == {1}
        assert len(assignment_counts) == 18

        membership_counts = {}
        for batch in status["batches"]:
            assert batch["status"] == "committed"
            for seq in batch["decision"]["request_seqs"]:
                membership_counts[seq] = membership_counts.get(seq, 0) + 1
        assert set(membership_counts.values()) == {1}
        assert len(membership_counts) == 18

        versions = {item["version"]: item for item in status["replica_versions"]}
        for window in status["windows"]:
            replica = next(
                item
                for item in versions[window["replica_version"]]["replicas"]
                if item["replica_id"] == window["replica_id"]
            )
            assert window["end_ms"] - window["start_ms"] == replica["batch_window_ms"]
            assert window["start_ms"] % replica["batch_window_ms"] == 0
            assert window["end_ms"] <= 300
            assert window["decision"]["window"]["replica_version"] == window["replica_version"]

        assert any(window["batch_count"] < 3 for window in status["windows"])
        assert any(
            batch["decision"]["priority_preemptions"] for batch in status["batches"]
        )
        for batch in status["batches"]:
            decision = batch["decision"]
            assert decision["first_constraint"]
            assert decision["observations"]["requests"]
            assert "rejected" in decision
        verification = verify_store(store)
        assert verification["ok"], verification["errors"]
    finally:
        store.close()


def test_fixed_point_error_ledger_and_precision_flags(tmp_path, sample_bundle) -> None:
    store, _ = _schedule_sample(tmp_path, sample_bundle)
    try:
        status = build_status(store)
        ledger = status["error_ledger"]
        assert ledger
        by_batch = {}
        for operation in ledger:
            by_batch.setdefault(operation["batch_id"], []).append(operation)
            assert operation["result_raw"] is not None
            assert operation["cumulative_error_bound"] >= 0
        for operations in by_batch.values():
            metrics = {operation["metric"] for operation in operations}
            assert metrics == {"compute", "memory", "latency"}
            conversions = [
                operation
                for operation in operations
                if operation["operation"] == "convert_fixed"
            ]
            assert len(conversions) == 3
            for conversion in conversions:
                assert conversion["scale"] > 0
                assert conversion["fixed_result"] is not None
                assert conversion["rounding"] in {"up", "down", "exact"}
            for metric in metrics:
                bounds = [
                    operation["cumulative_error_bound"]
                    for operation in operations
                    if operation["metric"] == metric
                ]
                assert bounds == sorted(bounds)
        assert status["precision_sensitive"]
        for flag in status["precision_sensitive"]:
            assert flag["depended"] is True
            assert flag["difference"] < flag["tolerance"]
            assert flag["kind"] in {"batch_plan", "batch_candidate", "replica_choice"}
        assert any(batch["precision_sensitive"] for batch in status["batches"])
    finally:
        store.close()


def test_repeated_schedule_is_idempotent(tmp_path, sample_bundle) -> None:
    store, _ = _schedule_sample(tmp_path, sample_bundle)
    try:
        first = build_status(store)
        with store.lock():
            second_run = SchedulerEngine(store).run(300)
        second = build_status(store)
        assert second_run["windows_committed"] == 0
        assert second_run["batches_committed"] == 0
        assert first["state_digest"] == second["state_digest"]
        assert first["windows"] == second["windows"]
        assert first["batches"] == second["batches"]
    finally:
        store.close()
