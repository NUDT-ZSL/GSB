from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from conftest import INJECTIONS, ROOT, SAMPLE, run_cli
from sequential_mq.errors import EngineError
from sequential_mq.model import validate_sample


def sample_object() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def validate_modified(tmp_path: Path, changed: dict) -> None:
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    validate_sample(path, injection_override=INJECTIONS / "none.json")


def test_valid_sample_has_deterministic_partition_routes() -> None:
    sample = validate_sample(SAMPLE, injection_override=INJECTIONS / "none.json")
    by_seq = {batch.batch_seq: batch for batch in sample.batches}
    orders = sample.topics_by_name["orders"]
    assert orders.route("customer-alice") == 0
    assert orders.route("customer-alice") == orders.route("customer-alice")
    assert by_seq[1].logical_ts_ms < by_seq[2].logical_ts_ms
    assert by_seq[2].tx_id == "tx-2001"
    assert by_seq[1].tx_id is None


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda value: value["topics"].append(copy.deepcopy(value["topics"][0])), "E_TOPIC_DUPLICATE"),
        (
            lambda value: value["config"].__setitem__("max_partitions_per_topic", 2),
            "E_PARTITION_LIMIT",
        ),
        (
            lambda value: value["topics"][0]["ordering_key_rules"][0].__setitem__(
                "algorithm", "md5"
            ),
            "E_ROUTING_RULE",
        ),
        (
            lambda value: value["batches"][1].__setitem__("batch_seq", 1),
            "E_BATCH_SEQ",
        ),
        (
            lambda value: value["batches"][1]["transaction"].__setitem__("declared", False),
            "E_TX_DECLARATION",
        ),
        (
            lambda value: value["batches"][0]["messages"][0]["payload"].__setitem__(
                "data", "not-base64%%"
            )
            or value["batches"][0]["messages"][0]["payload"].__setitem__(
                "encoding", "base64"
            ),
            "E_PAYLOAD_DECODE",
        ),
        (
            lambda value: value["batches"][0].__setitem__("logical_ts_ms", 900),
            "E_CLOCK_REGRESSION",
        ),
    ],
)
def test_invalid_samples_have_explicit_codes(
    tmp_path: Path,
    mutate,
    expected_code: str,
) -> None:
    value = sample_object()
    mutate(value)
    with pytest.raises(EngineError) as caught:
        validate_modified(tmp_path, value)
    assert caught.value.code == expected_code


def test_invalid_load_does_not_create_database(tmp_path: Path) -> None:
    value = sample_object()
    value["batches"][0]["messages"][0]["payload"] = {
        "encoding": "base64",
        "data": "!!!",
    }
    sample_path = tmp_path / "invalid.json"
    sample_path.write_text(json.dumps(value), encoding="utf-8")
    db = tmp_path / "should-not-exist.db"
    result = run_cli(
        db,
        "load",
        "--sample",
        str(sample_path),
        "--injections",
        str(INJECTIONS / "none.json"),
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_PAYLOAD_DECODE"
    assert not db.exists()
    assert not Path(str(db) + ".lock").exists()


def test_load_rejects_conflicting_topic_definition(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    first.write_text(json.dumps(sample_object()), encoding="utf-8")
    db = tmp_path / "state.db"
    result = run_cli(
        db,
        "load",
        "--sample",
        str(first),
        "--injections",
        str(INJECTIONS / "none.json"),
    )
    assert result.returncode == 0

    changed = sample_object()
    changed["topics"][0]["partition_capacities"][0] = 99
    second = tmp_path / "second.json"
    second.write_text(json.dumps(changed), encoding="utf-8")
    result = run_cli(
        db,
        "load",
        "--sample",
        str(second),
        "--injections",
        str(INJECTIONS / "none.json"),
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "E_CONFIG_CONFLICT"

