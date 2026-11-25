from __future__ import annotations

import copy
import sqlite3

import pytest

from health_machine.errors import HSMError
from health_machine.thresholds import load_v2_bundle, validate_load_v1
from health_machine.util import read_json, write_json
from health_machine.validation import validate_observations, validate_registry


def test_registry_error_codes_and_locations():
    base = {
        "services": [
            {"service_id": "s1", "service_name": "svc", "instances": [
                {"instance_id": "i1", "endpoint": "127.0.0.1:1000", "levels": [1, 2]}
            ]}
        ]
    }

    doc = copy.deepcopy(base)
    doc["services"][0]["instances"][0]["endpoint"] = "8.8.8.8:1000"
    with pytest.raises(HSMError) as exc:
        validate_registry(doc)
    assert exc.value.code == "HSM-1003"
    assert exc.value.location.instance_id == "i1"

    doc = copy.deepcopy(base)
    doc["services"][0]["instances"][0]["levels"] = [1, 1]
    with pytest.raises(HSMError) as exc:
        validate_registry(doc)
    assert exc.value.code == "HSM-1005"
    assert exc.value.location.level == 1

    doc = copy.deepcopy(base)
    doc["services"].append({"service_id": "s2", "service_name": "svc", "instances": []})
    with pytest.raises(HSMError) as exc:
        validate_registry(doc)
    assert exc.value.code == "HSM-1001"


def test_malformed_json_has_parse_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(HSMError) as exc:
        read_json(path)
    assert exc.value.code == "HSM-1000"
    assert "bad.json" in exc.value.location.path


def test_observation_error_codes_and_locations(workspace):
    registry = validate_registry(read_json(workspace.samples / "registry.json"))
    valid = read_json(workspace.samples / "observations.json")["observations"]

    bad_seq = [dict(item) for item in valid if item["instance_id"] == "inst-steady" and item["level"] == 1][:2]
    bad_seq[1]["seq"] = bad_seq[0]["seq"]
    with pytest.raises(HSMError) as exc:
        validate_observations(bad_seq, registry)
    assert exc.value.code == "HSM-1103"
    assert exc.value.location.instance_id == "inst-steady"
    assert exc.value.location.level == 1

    bad_clock = [dict(item) for item in bad_seq]
    bad_clock[1]["seq"] = bad_clock[0]["seq"] + 1
    bad_clock[1]["clock_ms"] = bad_clock[0]["clock_ms"] - 1
    with pytest.raises(HSMError) as exc:
        validate_observations(bad_clock, registry)
    assert exc.value.code == "HSM-1104"

    bad_count = [dict(valid[0])]
    bad_count[0]["successes"] = -1
    with pytest.raises(HSMError) as exc:
        validate_observations(bad_count, registry)
    assert exc.value.code == "HSM-1105"
    assert exc.value.location.level == valid[0]["level"]

    bad_latency = [dict(valid[0])]
    bad_latency[0]["avg_latency_ms"] = float("inf")
    with pytest.raises(HSMError) as exc:
        validate_observations(bad_latency, registry)
    assert exc.value.code == "HSM-1106"

    unknown_level = [dict(valid[0])]
    unknown_level[0]["level"] = 99
    with pytest.raises(HSMError) as exc:
        validate_observations(unknown_level, registry)
    assert exc.value.code == "HSM-1102"
    assert exc.value.location.level == 99


def test_v1_threshold_length_and_value_errors(workspace):
    registry = validate_registry(read_json(workspace.samples / "registry.json"))
    v1_path = workspace.samples / "thresholds_v1.json"
    doc = read_json(v1_path)
    doc["failure_thresholds"]["inst-steady"] = [1, 2]
    bad_path = workspace.root / "bad-v1-length.json"
    write_json(bad_path, doc)
    with pytest.raises(HSMError) as exc:
        validate_load_v1(bad_path, registry)
    assert exc.value.code == "HSM-1201"
    assert exc.value.location.instance_id == "inst-steady"

    doc = read_json(v1_path)
    doc["failure_thresholds"]["inst-steady"][0] = -1
    bad_value = workspace.root / "bad-v1-value.json"
    write_json(bad_value, doc)
    with pytest.raises(HSMError) as exc:
        validate_load_v1(bad_value, registry)
    assert exc.value.code == "HSM-1202"
    assert exc.value.location.level == 1


def test_v2_hash_generation_and_tombstone_errors(workspace):
    registry = validate_registry(read_json(workspace.samples / "registry.json"))
    v2_dir = workspace.samples / "thresholds_v2"
    shard_path = v2_dir / "shard-00.json"
    shard = read_json(shard_path)
    shard["thresholds"][0]["failure_limit"] = 12345
    write_json(shard_path, shard)
    with pytest.raises(HSMError) as exc:
        load_v2_bundle(v2_dir, v2_dir / "meta.sqlite", registry)
    assert exc.value.code == "HSM-1301"

    # Regenerate a fresh sample tree for metadata mutations.
    from health_machine.seed import generate_sample

    fresh = workspace.root / "fresh"
    generate_sample(fresh)
    fresh_registry = validate_registry(read_json(fresh / "registry.json"))
    meta = fresh / "thresholds_v2" / "meta.sqlite"
    conn = sqlite3.connect(meta)
    conn.execute("UPDATE v2_shards SET threshold_generation=99 WHERE shard_id='shard-00'")
    conn.commit()
    conn.close()
    with pytest.raises(HSMError) as exc:
        load_v2_bundle(fresh / "thresholds_v2", meta, fresh_registry)
    assert exc.value.code == "HSM-1302"

    conn = sqlite3.connect(meta)
    conn.execute("UPDATE v2_shards SET threshold_generation=2")
    conn.execute("INSERT INTO v2_tombstones VALUES ('inst-steady', 99, 100, 1, 'bad level')")
    conn.commit()
    conn.close()
    with pytest.raises(HSMError) as exc:
        load_v2_bundle(fresh / "thresholds_v2", meta, fresh_registry)
    assert exc.value.code == "HSM-1303"
    assert exc.value.location.level == 99
