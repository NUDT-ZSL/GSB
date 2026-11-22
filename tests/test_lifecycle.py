from __future__ import annotations

from dynabatch.scheduler import SchedulerEngine
from dynabatch.status import build_status
from dynabatch.store import Store
from dynabatch.training import TrainingEngine
from dynabatch.validation import validate_bundle


def test_execution_failure_increments_only_failure_counters(tmp_path, tiny_bundle) -> None:
    bundle = validate_bundle(tiny_bundle)
    with Store(tmp_path / "state.db", create=True) as store:
        store.ingest_bundle(bundle)
        with store.lock():
            SchedulerEngine(store).run(100)
        batch = store.fetchone(
            "SELECT batch_id FROM batches WHERE status = 'committed' ORDER BY batch_id"
        )
        assert batch is not None
        with store.lock():
            report = TrainingEngine(store).run(
                0, fail_batches=(str(batch["batch_id"]),)
            )
        assert report["batches_failed"] == 1
        status = build_status(store)
        assert status["stats"]["execution_failures"] == 1
        assert status["stats"]["expired_requests"] == 0
        assert all(
            request["failure_count"] == 1
            for request in status["requests"]
        )
        assert all(
            request["consecutive_failures"] == 1
            for request in status["requests"]
        )
        assert all(
            runtime["consecutive_failures"] == 1
            for runtime in status["replica_runtime"]
        )
        before = status["state_digest"]
        with store.lock():
            repeat = TrainingEngine(store).run(
                0, fail_batches=(str(batch["batch_id"]),)
            )
        assert repeat["batches_executed"] == 0
        assert build_status(store)["state_digest"] == before


def test_expiry_is_not_an_execution_failure(tmp_path, tiny_bundle) -> None:
    bundle = validate_bundle(tiny_bundle)
    with Store(tmp_path / "state.db", create=True) as store:
        store.ingest_bundle(bundle)
        store.execute("UPDATE requests SET deadline_ms = 2")
        with store.lock():
            SchedulerEngine(store).run(100)
            TrainingEngine(store).run(0)
        status = build_status(store)
        assert status["stats"]["expired_requests"] == 3
        assert status["stats"]["execution_failures"] == 0
        assert all(request["failure_count"] == 0 for request in status["requests"])
        assert all(
            runtime["consecutive_failures"] == 0
            for runtime in status["replica_runtime"]
        )


def test_persisted_running_batch_recovers_to_terminal_state(
    tmp_path, tiny_bundle
) -> None:
    bundle = validate_bundle(tiny_bundle)
    with Store(tmp_path / "state.db", create=True) as store:
        store.ingest_bundle(bundle)
        with store.lock():
            SchedulerEngine(store).run(100)
        batch = store.fetchone(
            "SELECT * FROM batches WHERE status = 'committed' ORDER BY batch_id LIMIT 1"
        )
        assert batch is not None
        batch_id = str(batch["batch_id"])
        start = int(batch["committed_clock_ms"])
        with store.transaction():
            store.execute(
                """
                UPDATE batches SET execution_status = 'running', started_clock_ms = ?
                WHERE batch_id = ?
                """,
                (start, batch_id),
            )
            store.execute(
                """
                UPDATE requests SET status = 'RUNNING'
                WHERE seq IN (SELECT request_seq FROM batch_members WHERE batch_id = ?)
                """,
                (batch_id,),
            )
        mid = build_status(store)
        assert mid["stats"]["request_status_counts"] == {"RUNNING": 3}
        with store.lock():
            TrainingEngine(store).run(0)
        status = build_status(store)
        assert status["stats"]["request_status_counts"] == {"COMPLETED": 3}
        assert status["batches"][0]["started_clock_ms"] == start
