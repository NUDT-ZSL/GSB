"""Validation chain: structured business codes, node attribution, and
no silent dropping of healthy jobs."""
from quant_conv_pipeline.errors import Code
from quant_conv_pipeline.sample_gen import read_jsonl
from quant_conv_pipeline.validation import validate_jobs


def _bad_jobs(data):
    return read_jsonl(f"{data}/bad_jobs.jsonl")


def test_each_bad_job_maps_to_its_code(ran):
    pipe = ran["pipe"]
    good = read_jsonl(f"{ran['data']}/jobs.jsonl")[:2]
    res = pipe.full_run(good + _bad_jobs(ran["data"]),
                        read_jsonl(f"{ran['data']}/weight_params.jsonl"),
                        read_jsonl(f"{ran['data']}/calib_stats.jsonl"))
    codes = {e["job_id"]: e["code"] for e in res["validation_errors"]}
    assert codes["job-000"] == Code.E_DUPLICATE_JOB_ID
    assert codes["bad-001"] == Code.E_SHAPE_MISMATCH
    assert codes["bad-002"] == Code.E_INVALID_QUANT_SCALE
    assert codes["bad-003"] == Code.E_INVALID_ZERO_POINT
    assert codes["bad-004"] == Code.E_UNSUPPORTED_ACC_DTYPE
    assert codes["job-005"] == Code.E_REVISION_ROLLBACK
    # healthy jobs still executed; bad ones excluded, not silently dropped
    assert set(res["lineage"].keys()) == {"job-000", "job-001"}
    for e in res["validation_errors"]:
        assert e["job_id"] and e["field"] and e["node"]


def test_validation_attributes_node(workspace):
    jobs = read_jsonl(f"{workspace['data']}/jobs.jsonl")
    _, errs = validate_jobs([dict(jobs[0], acc_dtype="float16")])
    assert errs[0]["code"] == Code.E_UNSUPPORTED_ACC_DTYPE
    assert errs[0]["node"] == "blocked_mac"
    _, errs = validate_jobs([dict(jobs[0], quant_scale=0.0)])
    assert errs[0]["node"] == "quant_input_prep"


def test_revision_rollback_in_commit(ran):
    pipe = ran["pipe"]
    res = pipe.commit([{"job_id": "job-001", "weight_revision": 1,
                        "weight_scale": 0.01, "weight_zero_point": 0}],
                      "weight")
    assert res["invalidation"]["affected_jobs"] == []
    assert res["validation_errors"][0]["code"] == Code.E_REVISION_ROLLBACK
    assert res["validation_errors"][0]["job_id"] == "job-001"
    assert res["validation_errors"][0]["details"]["current"] == 1
    assert res["validation_errors"][0]["details"]["arrived"] == 1


def test_update_for_unknown_job_rejected(ran):
    pipe = ran["pipe"]
    res = pipe.commit([{"job_id": "job-999", "calib_revision": 2,
                        "out_scale": 0.1, "out_zero_point": 0}], "calib")
    assert res["validation_errors"][0]["code"] == Code.E_BAD_MANIFEST
    assert res["invalidation"]["affected_jobs"] == []
