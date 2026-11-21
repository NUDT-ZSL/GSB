from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import INVALID, REGISTRY, SHARDS, run_cli
from probe_ledger.canonical import sha256_digest
from probe_ledger.errors import BusinessError
from probe_ledger.validation import parse_clock_file, parse_registry, parse_shard


def test_duplicate_service_rejected(sample_paths):
    with pytest.raises(BusinessError) as error:
        parse_registry(INVALID / "duplicate-service.json")
    assert error.value.code == "E_DUPLICATE_SERVICE"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("endpoint", "http://192.0.2.10:18101/health", "E_BAD_ENDPOINT"),
        ("expected_status_codes", [], "E_BAD_EXPECTED_STATUS"),
        ("lease_generation", -1, "E_BAD_LEASE"),
        ("state_generation", 0, "E_BAD_STATE_GENERATION"),
    ],
)
def test_registry_field_validation(tmp_path: Path, field, value, code):
    document = json.loads(REGISTRY.read_text(encoding="utf-8"))
    document["services"][0]["instances"][0][field] = value
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(BusinessError) as error:
        parse_registry(path)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("filename", "code"),
    [
        ("bad-observation-digest.json", "E_DIGEST_MISMATCH"),
        ("bad-shard-digest.json", "E_SHARD_DIGEST_MISMATCH"),
        ("unknown-instance.json", "E_UNKNOWN_INSTANCE"),
        ("truncated.json", "E_BAD_JSON"),
    ],
)
def test_invalid_shard_is_rejected_without_observation_write(tmp_path, filename, code):
    registry = parse_registry(REGISTRY)
    with pytest.raises(BusinessError) as error:
        parse_shard(INVALID / filename, registry.instances)
    assert error.value.code == code

    workspace = tmp_path / "ws"
    result = run_cli(
        "load",
        "--workspace",
        str(workspace),
        "--registry",
        str(REGISTRY),
        "--shard",
        str(INVALID / filename),
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["rejected"][0]["code"] == code
    with sqlite3.connect(workspace / "ledger.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM shards").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            == 0
        )


def test_bad_observation_sequence_and_clock_injection(tmp_path):
    registry = parse_registry(REGISTRY)
    shard_doc = json.loads((SHARDS[0]).read_text(encoding="utf-8"))
    observation = shard_doc["observations"][0]
    observation["obs_seq"] = 0
    shard_doc["shard_digest"] = sha256_digest(
        {key: value for key, value in shard_doc.items() if key != "shard_digest"}
    )
    path = tmp_path / "bad-seq.json"
    path.write_text(json.dumps(shard_doc), encoding="utf-8")
    with pytest.raises(BusinessError) as error:
        parse_shard(path, registry.instances)
    assert error.value.code == "E_BAD_OBS_SEQ"

    clean = json.loads((SHARDS[0]).read_text(encoding="utf-8"))
    clock_path = tmp_path / "clocks.json"
    clock_path.write_text(
        json.dumps(
            {
                "clocks": [
                    {
                        "instance_id": clean["observations"][0]["instance_id"],
                        "obs_seq": clean["observations"][0]["obs_seq"],
                        "logical_clock_ms": 999999,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    clocks = parse_clock_file(clock_path)
    with pytest.raises(BusinessError) as error:
        parse_shard(SHARDS[0], registry.instances, clocks=clocks)
    assert error.value.code == "E_CLOCK_INJECTION_MISMATCH"
