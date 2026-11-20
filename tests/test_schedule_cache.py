from __future__ import annotations

import copy
import json

from shardpipe.scheduler import run_schedule, status_report
from shardpipe.storage import StateStore
from shardpipe.verification import verify_run
from shardpipe.workload import parse_workload

from .conftest import store_with_document
from .conftest import one_dataset_document


def test_cache_reuse_across_runs_and_batch_idempotency(tmp_path, sample_document):
    store = store_with_document(tmp_path / "state", sample_document)
    workload = store.get_workload()
    job = workload.job_map()["train-alpha"]
    first = run_schedule(
        store=store, workload=workload, job=job, run_id="alpha-first"
    )
    assert first["status"] == "complete"
    assert first["result"]["cache_hits"] == 2
    assert first["result"]["cache_misses"] == 4
    assert first["result"]["committed_batches"] == 12
    assert verify_run(store, run_id="alpha-first")["ok"]

    second = run_schedule(
        store=store, workload=workload, job=job, run_id="alpha-second"
    )
    assert second["status"] == "complete"
    assert second["result"]["cache_hits"] == 6
    assert second["result"]["cache_misses"] == 0
    assert len(second["result"]["batch_cache_hits"]) == 12
    assert verify_run(store, run_id="alpha-second")["ok"]

    connection = store._connect()
    try:
        batch_results = connection.execute(
            "SELECT COUNT(*) AS count FROM batch_results"
        ).fetchone()["count"]
        run_batches = connection.execute(
            "SELECT COUNT(*) AS count FROM run_batches"
        ).fetchone()["count"]
    finally:
        connection.close()
    assert batch_results == 12
    assert run_batches == 24


def test_incremental_invalidation_is_dataset_precise(tmp_path, sample_document):
    store = store_with_document(tmp_path / "state", sample_document)
    workload = store.get_workload()
    for job_name, run_id in (("train-alpha", "a1"), ("train-beta", "b1")):
        result = run_schedule(
            store=store,
            workload=workload,
            job=workload.job_map()[job_name],
            run_id=run_id,
        )
        assert result["status"] == "complete"
    beta_before = {
        row["batch_id"]: row["batch_digest"]
        for row in store.list_run_batches("b1")
    }

    changed = copy.deepcopy(sample_document)
    changed["datasets"][0]["version"] = "v2"
    changed["datasets"][0]["generation_params"]["salt"] = "alpha-changed"
    changed_workload = parse_workload(changed)
    store.load(changed_workload)

    alpha = run_schedule(
        store=store,
        workload=changed_workload,
        job=changed_workload.job_map()["train-alpha"],
        run_id="a2",
    )
    assert alpha["status"] == "complete"
    assert alpha["result"]["cache_hits"] == 0
    assert alpha["result"]["cache_misses"] == 6
    invalidated_shards = {
        item["shard_index"] for item in alpha["result"]["cache_invalidated"]
    }
    assert invalidated_shards == set(range(6))
    assert alpha["result"]["batch_invalidations"]

    beta = run_schedule(
        store=store,
        workload=changed_workload,
        job=changed_workload.job_map()["train-beta"],
        run_id="b2",
    )
    assert beta["status"] == "complete"
    assert beta["result"]["cache_misses"] == 0
    assert beta["result"]["cache_hits"] == 4
    beta_after = {
        row["batch_id"]: row["batch_digest"]
        for row in store.list_run_batches("b2")
    }
    assert beta_before == beta_after
    assert len(beta["result"]["batch_cache_hits"]) == 8


def test_status_is_stable_and_reports_dataset_metrics(tmp_path, sample_document):
    store = store_with_document(tmp_path / "state", sample_document)
    workload = store.get_workload()
    run_schedule(
        store=store,
        workload=workload,
        job=workload.job_map()["train-alpha"],
        run_id="status-run",
    )
    first = status_report(store, "status-run")
    second = status_report(store, "status-run")
    assert first == second
    alpha = {
        item["dataset"]: item for item in first["datasets"]
    }["alpha"]
    assert alpha["cache_hit_rate"] == 2 / 6
    assert alpha["cache_hits"] == 2
    assert alpha["memory_bytes"] == 6144
    assert first["abandoned_shards"] == []


def test_batch_digest_is_recorded_once_across_epochs(tmp_path):
    document = one_dataset_document(
        sample_count=4,
        sample_bytes=64,
        shard_size=2,
        batch_size=2,
        epochs=2,
        prefetch_depth=1,
        memory_mib=8,
    )
    store = store_with_document(tmp_path / "state", document)
    workload = store.get_workload()
    result = run_schedule(
        store=store,
        workload=workload,
        job=workload.jobs[0],
        run_id="epoch-run",
    )
    assert result["status"] == "complete"
    connection = store._connect()
    try:
        batch_results = connection.execute(
            "SELECT COUNT(*) AS count FROM batch_results"
        ).fetchone()["count"]
        run_batches = connection.execute(
            "SELECT COUNT(*) AS count FROM run_batches"
        ).fetchone()["count"]
    finally:
        connection.close()
    assert batch_results == 2
    assert run_batches == 4
    assert verify_run(store, run_id="epoch-run")["ok"]


def test_verify_reports_first_divergent_sample_and_intermediate_states(
    tmp_path, sample_document
):
    store = store_with_document(tmp_path / "state", sample_document)
    workload = store.get_workload()
    run_schedule(
        store=store,
        workload=workload,
        job=workload.job_map()["train-alpha"],
        run_id="diff-run",
    )
    connection = store._connect()
    try:
        row = connection.execute(
            "SELECT batch_key, state_json FROM batch_results "
            "ORDER BY batch_key LIMIT 1"
        ).fetchone()
        state = json.loads(row["state_json"])
        state["sample_digests"][0] = "0" * 64
        connection.execute(
            "UPDATE batch_results SET state_json=? WHERE batch_key=?",
            (
                json.dumps(
                    state,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                row["batch_key"],
            ),
        )
    finally:
        connection.close()
    verified = verify_run(store, run_id="diff-run")
    assert verified["ok"] is False
    difference = verified["first_difference"]
    assert difference["kind"] == "batch_mismatch"
    assert difference["sample_index_in_batch"] == 0
    assert difference["expected_path_intermediate"]["sample_digest"]
    assert difference["actual_path_intermediate"]["sample_digest"] == "0" * 64
    assert verified["checks"]["batch_sample_summaries_match_serial_truth"] is False
