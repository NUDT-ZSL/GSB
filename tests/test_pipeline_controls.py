"""Concurrency control, resource limits, staleness and lineage."""
import os

import pytest

from quant_conv_pipeline.errors import Code, PipelineError
from quant_conv_pipeline.pipeline import CommitLock
from quant_conv_pipeline.sample_gen import read_jsonl


def test_stage_busy_is_immediate(ran):
    pipe = ran["pipe"]
    lock = CommitLock(pipe.cfg.state_dir).__enter__()
    try:
        with pytest.raises(PipelineError) as ei:
            pipe.commit([{"job_id": "job-001", "weight_revision": 2,
                          "weight_scale": 0.01, "weight_zero_point": 0}],
                        "weight")
        assert ei.value.code == Code.E_STAGE_BUSY
    finally:
        lock.__exit__()


def test_resource_limit_recompute_jobs(ran):
    pipe = ran["pipe"]
    pipe.cfg.limits.max_recompute_jobs = 1
    rows = [{"job_id": j, "calib_revision": 2, "out_scale": 0.1,
             "out_zero_point": 0} for j in ("job-000", "job-001")]
    with pytest.raises(PipelineError) as ei:
        pipe.commit(rows, "calib")
    assert ei.value.code == Code.E_RESOURCE_LIMIT
    assert ei.value.details["affected"] == 2
    assert ei.value.details["limit"] == 1


def test_block_size_degradation_marked(ran):
    pipe = ran["pipe"]
    pipe.cfg.limits.max_block_resident_bytes = 1024
    res = pipe.commit([{"job_id": "job-004", "weight_revision": 2,
                        "weight_scale": 0.011, "weight_zero_point": 1}],
                      "weight")
    degs = [d for d in res["degradations"]
            if d["category"] == "BLOCK_SIZE_DEGRADED"]
    assert degs and degs[0]["job_id"] == "job-004"
    assert degs[0]["to_block_size"] < degs[0]["from_block_size"]


def test_parallelism_degradation_marked(ran):
    pipe = ran["pipe"]
    pipe.cfg.limits.max_parallel_nodes = 2
    res = pipe.commit(read_jsonl(f"{ran['data']}/updates/"
                                 "weight_params_v2.jsonl"), "weight")
    degs = [d for d in res["degradations"]
            if d["category"] == "PARALLELISM_DEGRADED"]
    assert degs and degs[0]["to_parallel"] == 2


def test_stale_result_rejected_with_version_diff(ran):
    pipe = ran["pipe"]
    pipe.commit(read_jsonl(f"{ran['data']}/updates/"
                           "weight_params_v2.jsonl"), "weight")
    with pytest.raises(PipelineError) as ei:
        pipe.check_result("snap-0001", "job-002")
    err = ei.value
    assert err.code == Code.E_STALE_RESULT
    assert err.job_id == "job-002"
    assert err.details["version_diff"]["weight_revision"] == \
        {"recorded": 1, "current": 2}


def test_fresh_result_replays_identically(ran):
    pipe = ran["pipe"]
    out = pipe.check_result("snap-0001", "job-001")
    assert out["replayed"] and out["stale"] is False


def test_lineage_broken_for_unknown_job(ran):
    pipe = ran["pipe"]
    with pytest.raises(PipelineError) as ei:
        pipe.check_result("snap-0001", "job-999")
    assert ei.value.code == Code.E_LINEAGE_BROKEN
    assert ei.value.job_id == "job-999"


def test_lineage_broken_when_upstream_evicted(ran):
    pipe = ran["pipe"]
    rec = pipe.load_snapshot("snap-0001")["lineage"]["job-000"]
    mac_key = rec["node_keys"]["blocked_mac"]
    meta = pipe.cache.meta(mac_key)
    n1_key = meta["payload"]["upstream"]["quant_input_prep"]
    os.remove(pipe.cache._paths(n1_key)[0])
    from quant_conv_pipeline import nodes
    with pytest.raises(PipelineError) as ei:
        nodes.replay(pipe.cache, mac_key)
    assert ei.value.code == Code.E_LINEAGE_BROKEN
    assert ei.value.job_id == "job-000"
    assert ei.value.node == "blocked_mac"


def test_second_full_run_reuses_everything(ran):
    pipe = ran["pipe"]
    res = pipe.full_run(read_jsonl(f"{ran['data']}/jobs.jsonl"),
                        read_jsonl(f"{ran['data']}/weight_params.jsonl"),
                        read_jsonl(f"{ran['data']}/calib_stats.jsonl"))
    assert len(res["invalidation"]["reuse"]) == 12 * 4
    assert res["invalidation"]["recompute"] == []
