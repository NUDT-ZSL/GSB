from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from shardpipe.cli import main
from shardpipe.errors import BusinessError, ErrorCode
from shardpipe.storage import StateStore
from shardpipe.workload import parse_workload

from .conftest import one_dataset_document, write_document


def test_duplicate_dataset_and_job_are_rejected(sample_document):
    duplicate_dataset = copy.deepcopy(sample_document)
    duplicate_dataset["datasets"].append(
        copy.deepcopy(duplicate_dataset["datasets"][0])
    )
    with pytest.raises(BusinessError) as exc:
        parse_workload(duplicate_dataset)
    assert exc.value.code == ErrorCode.DUPLICATE_DATASET.value

    duplicate_job = copy.deepcopy(sample_document)
    duplicate_job["jobs"].append(copy.deepcopy(duplicate_job["jobs"][0]))
    with pytest.raises(BusinessError) as exc:
        parse_workload(duplicate_job)
    assert exc.value.code == ErrorCode.DUPLICATE_JOB.value


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("sample_count", 0, ErrorCode.INVALID_SAMPLE_COUNT),
        ("feature_dim", 0, ErrorCode.INVALID_FEATURE_DIM),
        ("shard_size", 5, ErrorCode.SHARD_SIZE_NOT_DIVISIBLE),
        ("sample_bytes", 8, ErrorCode.INVALID_SAMPLE_BYTES),
    ],
)
def test_dataset_numeric_validation(field, value, code):
    document = one_dataset_document()
    document["datasets"][0][field] = value
    with pytest.raises(BusinessError) as exc:
        parse_workload(document)
    assert exc.value.code == code.value


def test_job_validation():
    for field, value, code in [
        ("data_parallelism", 0, ErrorCode.INVALID_DATA_PARALLELISM),
        ("prefetch_depth", -1, ErrorCode.INVALID_PREFETCH_DEPTH),
        ("batch_size", 0, ErrorCode.INVALID_BATCH_SIZE),
        ("epochs", 0, ErrorCode.INVALID_EPOCHS),
        ("allow_shard_reuse", 1, ErrorCode.INVALID_BOOLEAN),
    ]:
        document = one_dataset_document()
        document["jobs"][0][field] = value
        with pytest.raises(BusinessError) as exc:
            parse_workload(document)
        assert exc.value.code == code.value


def test_cache_bytes_and_digest_validation(sample_document):
    bad_bytes = copy.deepcopy(sample_document)
    bad_bytes["cache"]["entries"][0]["bytes"] += 1
    with pytest.raises(BusinessError) as exc:
        parse_workload(bad_bytes)
    assert exc.value.code == ErrorCode.CACHE_BYTES_MISMATCH.value

    bad_digest = copy.deepcopy(sample_document)
    bad_digest["cache"]["entries"][0]["digest"] = "0" * 64
    with pytest.raises(BusinessError) as exc:
        parse_workload(bad_digest)
    assert exc.value.code == ErrorCode.CACHE_DIGEST_MISMATCH.value


def test_invalid_load_does_not_create_state(tmp_path, sample_document, capsys):
    document = copy.deepcopy(sample_document)
    document["datasets"][0]["sample_count"] = 95
    path = write_document(tmp_path / "bad.json", document)
    state_dir = tmp_path / "state-not-created"
    code = main(
        ["load", "--input", str(path), "--state-dir", str(state_dir)]
    )
    assert code == 2
    assert not state_dir.exists()
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == ErrorCode.SHARD_SIZE_NOT_DIVISIBLE.value


def test_invalid_schedule_input_does_not_create_state(
    tmp_path, sample_document, capsys
):
    document = copy.deepcopy(sample_document)
    document["datasets"][0]["shard_size"] = 7
    path = write_document(tmp_path / "bad-schedule.json", document)
    state_dir = tmp_path / "schedule-state-not-created"
    code = main(
        [
            "schedule",
            "--input",
            str(path),
            "--state-dir",
            str(state_dir),
            "--job",
            "train-alpha",
        ]
    )
    assert code == 2
    assert not state_dir.exists()
    assert json.loads(capsys.readouterr().err)["error"]["code"] == (
        ErrorCode.SHARD_SIZE_NOT_DIVISIBLE.value
    )


def test_load_enforces_cache_capacity(tmp_path, sample_document):
    document = copy.deepcopy(sample_document)
    document["scheduling"]["cache_capacity_entries"] = 2
    workload = parse_workload(document)
    store = StateStore(tmp_path / "state")
    summary = store.load(workload)
    assert summary["cache_entries_evicted"]
    assert len(store.list_cache_entries()) == 2


def test_cache_generation_regression_rolls_back(tmp_path, sample_document):
    store = StateStore(tmp_path / "state")
    original = parse_workload(copy.deepcopy(sample_document))
    store.load(original)
    regressed_document = copy.deepcopy(sample_document)
    regressed_document["cache"]["generation"] = 2
    for entry in regressed_document["cache"]["entries"]:
        entry["generation"] = 2
    regressed = parse_workload(regressed_document)
    with pytest.raises(BusinessError) as exc:
        store.load(regressed)
    assert exc.value.code == ErrorCode.CACHE_GENERATION_REGRESSION.value
    connection = store._connect()
    try:
        workload_count = connection.execute(
            "SELECT COUNT(*) AS count FROM workloads"
        ).fetchone()["count"]
        meta = connection.execute(
            "SELECT generation FROM cache_meta"
        ).fetchone()["generation"]
    finally:
        connection.close()
    assert workload_count == 1
    assert meta == original.cache.generation
