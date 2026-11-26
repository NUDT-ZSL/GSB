from __future__ import annotations

from pathlib import Path

import pytest

from cold_diag.errors import PolicyIntegrityError, SampleValidationError
from cold_diag.parser import (
    parse_function_specs,
    parse_requests,
    parse_v1_policy,
    load_v2_package,
)
from cold_diag.policy import merge_policy_records
from cold_diag.sample_data import generate_sample
from cold_diag.utils import read_json, write_json


def test_function_validation_rejects_duplicate_and_non_positive() -> None:
    with pytest.raises(SampleValidationError, match="duplicate"):
        parse_function_specs(
            {
                "functions": [
                    {"name": "a", "cold_start_ms": 1, "concurrency_limit": 1},
                    {"name": "a", "cold_start_ms": 2, "concurrency_limit": 1},
                ]
            }
        )
    with pytest.raises(SampleValidationError, match="cold_start_ms"):
        parse_function_specs(
            {"functions": [{"name": "b", "cold_start_ms": 0, "concurrency_limit": 1}]}
        )
    with pytest.raises(SampleValidationError, match="concurrency_limit"):
        parse_function_specs(
            {"functions": [{"name": "c", "cold_start_ms": 1, "concurrency_limit": 0}]}
        )


def test_request_validation_requires_strict_seq_and_non_negative_clock() -> None:
    functions = parse_function_specs(
        {"functions": [{"name": "a", "cold_start_ms": 1, "concurrency_limit": 1}]}
    )
    names = [item.name for item in functions]
    with pytest.raises(SampleValidationError, match="strictly increasing"):
        parse_requests(
            {
                "requests": [
                    {"seq": 2, "function_name": "a", "logical_clock_ms": 0},
                    {"seq": 2, "function_name": "a", "logical_clock_ms": 1},
                ]
            },
            names,
        )
    with pytest.raises(SampleValidationError, match="logical clock"):
        parse_requests(
            {"requests": [{"seq": 1, "function_name": "a", "logical_clock_ms": -1}]},
            names,
        )


def test_v1_relative_seconds_must_be_non_negative() -> None:
    with pytest.raises(SampleValidationError, match="effective_relative_seconds"):
        parse_v1_policy(
            {
                "declared_clock_ms": 10,
                "policies": [
                    {"key": "k", "effective_relative_seconds": -1, "generation": 1}
                ],
            }
        )


def test_generated_sample_v2_hashes_and_merge_decisions(tmp_path: Path) -> None:
    root = generate_sample(tmp_path / "data")
    records, manifest, metadata = load_v2_package(Path(root["policies_v2_manifest"]).parent)
    assert manifest["version"] == 2
    assert len(metadata) == len(records)

    v1 = parse_v1_policy(read_json(root["policies_v1"]))
    merged = merge_policy_records(v1 + records)
    alpha = merged["pool:alpha"]
    assert alpha.selected.format_generation == "v2"
    assert alpha.selected.generation == 2
    assert alpha.decision["winner_original_time"]["kind"] == "absolute_milliseconds"

    beta = merged["pool:beta"]
    assert beta.selected.format_generation == "v2"
    assert "equal_effective_logical_clock" in " ".join(beta.decision["tie_breakers"])


def test_v2_shard_hash_tamper_is_rejected(tmp_path: Path) -> None:
    root = Path(generate_sample(tmp_path / "data")["root"])
    shard = root / "policies_v2" / "shards" / "shard-000.json"
    payload = read_json(shard)
    payload["records"][0]["effective_at_ms"] += 1
    write_json(shard, payload)
    with pytest.raises(PolicyIntegrityError, match="hash mismatch"):
        load_v2_package(root / "policies_v2")
